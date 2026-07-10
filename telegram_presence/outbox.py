"""Crash-safe, stdlib-only outbound delivery queue.

The outbox provides at-least-once delivery. A process crash after the remote
transport accepts a message but before the local ACK is persisted can result
in a duplicate; correlation and idempotency fields make that case observable.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from telegram_presence.delivery import (
    DeliveryRecord,
    DeliveryState,
    MessageEnvelope,
    TransportReceipt,
)

try:  # pragma: no cover - exercised on Unix; fallback keeps import portable
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


Sender = Callable[[MessageEnvelope], Any]
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
_RECORD_SUFFIX = ".delivery.json"


class OutboxCorruptionError(RuntimeError):
    """A persisted record cannot be decoded or violates the schema."""


class DurableOutbox:
    """Filesystem outbox with leases, bounded backoff, and crash recovery."""

    def __init__(
        self,
        root: os.PathLike[str] | str,
        *,
        max_attempts: int = 5,
        base_retry_seconds: float = 1.0,
        max_retry_seconds: float = 300.0,
        sending_timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
        recover: bool = True,
    ) -> None:
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or max_attempts < 1):
            raise ValueError("max_attempts must be a positive integer")
        for name, value in (("base_retry_seconds", base_retry_seconds),
                            ("max_retry_seconds", max_retry_seconds),
                            ("sending_timeout_seconds", sending_timeout_seconds)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or value <= 0):
                raise ValueError(f"{name} must be positive")
        if max_retry_seconds < base_retry_seconds:
            raise ValueError("max_retry_seconds cannot be less than base_retry_seconds")
        if not callable(clock):
            raise ValueError("clock must be callable")

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.max_attempts = max_attempts
        self.base_retry_seconds = float(base_retry_seconds)
        self.max_retry_seconds = float(max_retry_seconds)
        self.sending_timeout_seconds = float(sending_timeout_seconds)
        self._clock = clock
        self._mutex = threading.RLock()
        self._lock_path = self.root / ".outbox.lock"
        self._lock_path.touch(mode=0o600, exist_ok=True)
        try:
            self._lock_path.chmod(0o600)
        except OSError:
            pass
        if recover:
            self.recover_inflight()
            self._enforce_attempt_limits()

    @contextmanager
    def _locked(self):
        with self._mutex:
            with self._lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _now(self) -> float:
        raw = self._clock()
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("clock returned an invalid timestamp")
        now = float(raw)
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock returned an invalid timestamp")
        return now

    def _retry_delay(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        try:
            candidate = math.ldexp(self.base_retry_seconds, exponent)
        except (OverflowError, ValueError):
            return self.max_retry_seconds
        return min(self.max_retry_seconds, candidate)

    def _path(self, delivery_id: str) -> Path:
        if not isinstance(delivery_id, str) or not _SAFE_ID.fullmatch(delivery_id):
            raise ValueError("invalid delivery_id")
        return self.root / f"{delivery_id}{_RECORD_SUFFIX}"

    def _read_unlocked(self, path: Path) -> DeliveryRecord:
        try:
            with path.open("r", encoding="utf-8") as source:
                value = json.load(source)
            return DeliveryRecord.from_dict(value)
        except Exception as exc:
            raise OutboxCorruptionError(f"cannot read outbox record {path.name}") from exc

    def _get_unlocked(self, delivery_id: str) -> Optional[DeliveryRecord]:
        path = self._path(delivery_id)
        return self._read_unlocked(path) if path.exists() else None

    def _records_unlocked(self) -> list[DeliveryRecord]:
        records = [self._read_unlocked(path)
                   for path in sorted(self.root.glob(f"*{_RECORD_SUFFIX}"))]
        records.sort(key=lambda record: (record.created_at, record.delivery_id))
        return records

    def _write_unlocked(self, record: DeliveryRecord) -> None:
        target = self._path(record.delivery_id)
        payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        temporary_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=".outbox-", suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary_name, 0o600)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _delivery_id(envelope: MessageEnvelope) -> str:
        return uuid5(
            NAMESPACE_URL,
            f"telegram-presence:{envelope.transport}:{envelope.idempotency_key}",
        ).hex

    @staticmethod
    def _request_payload(envelope: MessageEnvelope) -> dict:
        value = envelope.to_dict()
        for key in ("schema_version", "envelope_id", "correlation_id", "created_at"):
            value.pop(key, None)
        return value

    def enqueue(self, envelope: MessageEnvelope) -> DeliveryRecord:
        """Persist an intent before any transport call.

        Reusing an idempotency key returns the existing record. Reusing it for
        a different request is rejected instead of silently changing history.
        """
        if not isinstance(envelope, MessageEnvelope):
            raise ValueError("envelope must be a MessageEnvelope")
        delivery_id = self._delivery_id(envelope)
        now = self._now()
        with self._locked():
            existing = self._get_unlocked(delivery_id)
            if existing is not None:
                if self._request_payload(existing.envelope) != self._request_payload(envelope):
                    raise ValueError("idempotency_key is already bound to another request")
                return existing
            record = DeliveryRecord(
                envelope=envelope,
                state=DeliveryState.PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                delivery_id=delivery_id,
            )
            self._write_unlocked(record)
            return record

    def get(self, delivery_id: str) -> Optional[DeliveryRecord]:
        with self._locked():
            return self._get_unlocked(delivery_id)

    def list(self, states: Optional[Iterable[DeliveryState | str]] = None) -> list[DeliveryRecord]:
        wanted = None
        if states is not None:
            wanted = {item if isinstance(item, DeliveryState) else DeliveryState(item)
                      for item in states}
        with self._locked():
            records = self._records_unlocked()
        return records if wanted is None else [record for record in records
                                                if record.state in wanted]

    def _claim_unlocked(self, record: DeliveryRecord, now: float) -> DeliveryRecord:
        now = max(now, record.updated_at)
        claimed = replace(
            record,
            state=DeliveryState.SENDING,
            attempts=record.attempts + 1,
            next_attempt_at=None,
            updated_at=now,
            sending_started_at=now,
            acked_at=None,
            last_error=None,
            lease_id=uuid4().hex,
        )
        self._write_unlocked(claimed)
        return claimed

    def _dead_letter_exhausted_unlocked(
        self, record: DeliveryRecord, now: float,
    ) -> DeliveryRecord:
        now = max(now, record.updated_at)
        detail = f"attempt limit reached ({record.attempts}/{self.max_attempts})"
        if record.last_error:
            detail = f"{detail}; last error: {record.last_error}"
        updated = replace(
            record,
            state=DeliveryState.DEAD_LETTER,
            next_attempt_at=None,
            updated_at=now,
            sending_started_at=None,
            acked_at=None,
            last_error=detail[:1000],
            lease_id=None,
        )
        self._write_unlocked(updated)
        return updated

    def _enforce_attempt_limits(self) -> list[DeliveryRecord]:
        """Normalize retryable records after a lower max-attempts config."""
        now = self._now()
        updated: list[DeliveryRecord] = []
        with self._locked():
            for record in self._records_unlocked():
                if (record.state in (DeliveryState.PENDING, DeliveryState.FAILED)
                        and record.attempts >= self.max_attempts):
                    updated.append(self._dead_letter_exhausted_unlocked(record, now))
        return updated

    def _claim_due_transports(
        self, transports: Optional[set[str]] = None,
    ) -> Optional[DeliveryRecord]:
        now = self._now()
        with self._locked():
            due = []
            for record in self._records_unlocked():
                if record.state not in (DeliveryState.PENDING, DeliveryState.FAILED):
                    continue
                if transports is not None and record.transport not in transports:
                    continue
                if record.attempts >= self.max_attempts:
                    self._dead_letter_exhausted_unlocked(record, now)
                    continue
                if record.next_attempt_at is None or record.next_attempt_at <= now:
                    due.append(record)
            if not due:
                return None
            due.sort(key=lambda record: (record.next_attempt_at or 0,
                                         record.created_at, record.delivery_id))
            return self._claim_unlocked(due[0], now)

    def claim_due(self, *, transport: Optional[str] = None) -> Optional[DeliveryRecord]:
        """Atomically lease the oldest due delivery, optionally by transport."""
        transports = None
        if transport is not None:
            if not isinstance(transport, str) or not transport.strip():
                raise ValueError("transport must be a non-empty string")
            transports = {transport.strip().lower()}
        return self._claim_due_transports(transports)

    def _claim_id(self, delivery_id: str) -> Optional[DeliveryRecord]:
        now = self._now()
        with self._locked():
            record = self._get_unlocked(delivery_id)
            if record is None or record.state not in (DeliveryState.PENDING,
                                                       DeliveryState.FAILED):
                return None
            if record.attempts >= self.max_attempts:
                self._dead_letter_exhausted_unlocked(record, now)
                return None
            if record.next_attempt_at is not None and record.next_attempt_at > now:
                return None
            return self._claim_unlocked(record, now)

    @staticmethod
    def _receipt(result: Any, envelope: MessageEnvelope) -> TransportReceipt:
        if isinstance(result, TransportReceipt):
            if result.transport != envelope.transport:
                raise ValueError("transport receipt does not match envelope transport")
            if result.correlation_id != envelope.correlation_id:
                raise ValueError("transport receipt does not match correlation_id")
            return result
        if isinstance(result, bool):
            return TransportReceipt(
                success=result,
                transport=envelope.transport,
                correlation_id=envelope.correlation_id,
                error=None if result else "transport returned false",
            )
        if isinstance(result, Mapping):
            marker = result.get("success", result.get("ok"))
            if not isinstance(marker, bool):
                raise ValueError("transport mapping must contain boolean success or ok")
            receipt = TransportReceipt(
                success=marker,
                transport=str(result.get("transport") or envelope.transport),
                correlation_id=str(result.get("correlation_id")
                                   or envelope.correlation_id),
                transport_message_id=result.get("transport_message_id")
                                     or result.get("message_id"),
                error=result.get("error"),
            )
            if receipt.transport != envelope.transport:
                raise ValueError("transport receipt does not match envelope transport")
            if receipt.correlation_id != envelope.correlation_id:
                raise ValueError("transport receipt does not match correlation_id")
            return receipt
        raise ValueError("transport must return bool, mapping, or TransportReceipt")

    def _settle(
        self,
        claimed: DeliveryRecord,
        *,
        receipt: Optional[TransportReceipt] = None,
        error: Optional[str] = None,
    ) -> DeliveryRecord:
        now = self._now()
        with self._locked():
            current = self._get_unlocked(claimed.delivery_id)
            if current is None:
                raise OutboxCorruptionError("claimed delivery disappeared")
            if (current.state != DeliveryState.SENDING
                    or current.lease_id != claimed.lease_id):
                return current
            now = max(now, current.updated_at)

            if receipt is not None and receipt.success:
                settled = replace(
                    current,
                    state=DeliveryState.ACKED,
                    next_attempt_at=None,
                    updated_at=now,
                    sending_started_at=None,
                    acked_at=now,
                    last_error=None,
                    transport_message_id=receipt.transport_message_id,
                    lease_id=None,
                )
            else:
                message = error or (receipt.error if receipt else None) or "transport failed"
                exhausted = current.attempts >= self.max_attempts
                delay = None if exhausted else self._retry_delay(current.attempts)
                settled = replace(
                    current,
                    state=(DeliveryState.DEAD_LETTER if exhausted
                           else DeliveryState.FAILED),
                    next_attempt_at=None if exhausted else now + (delay or 0),
                    updated_at=now,
                    sending_started_at=None,
                    acked_at=None,
                    last_error=str(message)[:1000],
                    lease_id=None,
                )
            self._write_unlocked(settled)
            return settled

    def _send_claimed(self, claimed: DeliveryRecord, sender: Sender) -> DeliveryRecord:
        try:
            receipt = self._receipt(sender(claimed.envelope), claimed.envelope)
        except Exception as exc:
            return self._settle(claimed, error=f"{type(exc).__name__}: {exc}")
        return self._settle(claimed, receipt=receipt)

    def dispatch_one(self, delivery_id: str, sender: Sender) -> Optional[DeliveryRecord]:
        """Attempt one due delivery, returning ``None`` when it is not due."""
        if not callable(sender):
            raise ValueError("sender must be callable")
        self.recover_inflight()
        claimed = self._claim_id(delivery_id)
        return None if claimed is None else self._send_claimed(claimed, sender)

    def dispatch_due(
        self,
        sender: Sender | Mapping[str, Sender],
        *,
        limit: int = 100,
        transport: Optional[str] = None,
    ) -> list[DeliveryRecord]:
        """Attempt due records with one sender or a transport sender map.

        A bound bundled adapter is filtered automatically via its
        ``transport_name``. For a shared multi-transport root, pass a mapping
        such as ``{"bot_api": bot.send_envelope, "telethon": user.send_envelope}``.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        routes: Optional[dict[str, Sender]] = None
        transport_filter: Optional[set[str]] = None
        if isinstance(sender, Mapping):
            if transport is not None:
                raise ValueError("transport cannot be combined with a sender mapping")
            routes = {}
            for name, route in sender.items():
                if not isinstance(name, str) or not name.strip() or not callable(route):
                    raise ValueError("sender mapping requires transport-name/callable pairs")
                routes[name.strip().lower()] = route
            if not routes:
                raise ValueError("sender mapping cannot be empty")
            transport_filter = set(routes)
        else:
            if not callable(sender):
                raise ValueError("sender must be callable or a transport mapping")
            selected = transport
            if selected is None:
                owner = getattr(sender, "__self__", None)
                selected = getattr(owner, "transport_name", None)
            if selected is not None:
                if not isinstance(selected, str) or not selected.strip():
                    raise ValueError("transport must be a non-empty string")
                transport_filter = {selected.strip().lower()}
        self.recover_inflight()
        settled: list[DeliveryRecord] = []
        for _ in range(limit):
            claimed = self._claim_due_transports(transport_filter)
            if claimed is None:
                break
            route = routes[claimed.transport] if routes is not None else sender
            settled.append(self._send_claimed(claimed, route))
        return settled

    def recover_inflight(self, *, force: bool = False) -> list[DeliveryRecord]:
        """Move abandoned ``sending`` leases back to retryable state.

        Normal recovery waits for ``sending_timeout_seconds`` so two live
        workers cannot steal each other's lease. ``force=True`` is intended
        for a controlled single-worker restart.
        """
        now = self._now()
        recovered: list[DeliveryRecord] = []
        with self._locked():
            for record in self._records_unlocked():
                if record.state != DeliveryState.SENDING:
                    continue
                started = record.sending_started_at
                stale = started is None or started <= now - self.sending_timeout_seconds
                if not force and not stale:
                    continue
                exhausted = record.attempts >= self.max_attempts
                record_now = max(now, record.updated_at)
                updated = replace(
                    record,
                    state=(DeliveryState.DEAD_LETTER if exhausted
                           else DeliveryState.FAILED),
                    next_attempt_at=None if exhausted else record_now,
                    updated_at=record_now,
                    sending_started_at=None,
                    last_error="recovered interrupted sending lease",
                    lease_id=None,
                )
                self._write_unlocked(updated)
                recovered.append(updated)
        return recovered

    def requeue_dead_letter(self, delivery_id: str) -> DeliveryRecord:
        """Explicitly reset one dead-letter record for operator retry."""
        now = self._now()
        with self._locked():
            record = self._get_unlocked(delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            if record.state != DeliveryState.DEAD_LETTER:
                raise ValueError("delivery is not in dead_letter")
            now = max(now, record.updated_at)
            updated = replace(
                record,
                state=DeliveryState.FAILED,
                attempts=0,
                next_attempt_at=now,
                updated_at=now,
                last_error="operator requeued dead letter",
                lease_id=None,
            )
            self._write_unlocked(updated)
            return updated

    def purge_acked(self, *, before: Optional[float] = None) -> int:
        """Delete ACKed records, optionally only those ACKed before a time."""
        removed = 0
        with self._locked():
            for record in self._records_unlocked():
                if record.state != DeliveryState.ACKED:
                    continue
                if before is not None and (record.acked_at is None or record.acked_at >= before):
                    continue
                self._path(record.delivery_id).unlink(missing_ok=True)
                removed += 1
        return removed
