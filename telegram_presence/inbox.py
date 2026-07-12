"""Realtime group-mention inbox: addressed detection + bounded jsonl spool.

Fed by a live transport (Bot API / Telethon); consumed by the engage organ.
No LLM here; group text is untrusted evidence — sanitized snippets only.

Persistence is fail-closed: a row is only ACKable upstream after it has been
flushed and fsynced (``InboxAddResult.safe_to_ack``), a torn final line is
repaired before the next append, and recent durable ids are re-hydrated on
restart so a transport replay is a no-op. Every appended row carries a
monotonic ``spool_seq`` (crash-safe sidecar counter under the same lock), so
queue consumers can use an exact sequence cursor instead of wall-clock
timestamps that collide inside one second.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime
from dataclasses import dataclass
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - Unix path is exercised; fallback is single-process
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger(__name__)

SPOOL_REL = Path("state") / "telegram_group_inbox.jsonl"
RECEIPT_REL = Path("state") / "telegram_addressed_mentions_monitor.json"
SPOOL_SEQ_REL = Path("state") / "telegram_group_inbox.seq"
MAX_SPOOL_BYTES = 1_000_000
MAX_SPOOL_RETAIN_ROWS = 500
MAX_SNIPPET_CHARS = 500
MAX_ADDRESSED_FULL_TEXT_CHARS = 4096
MAX_SEEN_IDS = 2048
MAX_OWN_IDS = 200

DEFAULT_NAMES = ("rain", "rain_ouroboros", "рейн", "рэйн", "ouroboros", "ороборос")
INFLECTED_ROOTS = ("рейн", "рэйн", "ороборос")
_CYR = "а-яёА-ЯЁ"


@dataclass(frozen=True, slots=True)
class InboxAddResult:
    """Outcome of one inbox write attempt.

    ``safe_to_ack`` means an upstream transport may advance its cursor: the
    row was durably written, was already present, or was intentionally ignored.
    A storage failure is the only outcome that must leave the cursor in place.
    """

    written: bool
    safe_to_ack: bool
    reason: str


def _compile_term_patterns(term: str) -> list[tuple[str, re.Pattern[str]]]:
    escaped = re.escape(term.strip())
    if not escaped:
        return []
    flags = re.IGNORECASE
    return [
        (f"@{term}", re.compile(rf"(?<!\w)@{escaped}(?!\w)", flags)),
        (term, re.compile(rf"(?<!\w){escaped}(?!\w)", flags)),
    ]


def _inflected_label(text: str, root: str) -> str | None:
    escaped = re.escape(root)
    pattern = re.compile(rf"(?<![\w{_CYR}]){escaped}[{_CYR}]{{0,3}}(?![{_CYR}])", re.IGNORECASE)
    return root if pattern.search(text) else None


def matched_terms(text: str | None, names: tuple[str, ...] | None = None) -> list[str]:
    if names is None:
        from .hooks import name_terms
        names = name_terms()
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        for label, pattern in _compile_term_patterns(raw_name.strip()):
            key = label.casefold()
            if key not in seen and pattern.search(text):
                found.append(label)
                seen.add(key)
    for root in INFLECTED_ROOTS:
        label = _inflected_label(text or "", root)
        if label is not None and label.casefold() not in seen:
            found.append(label)
            seen.add(label.casefold())
    return found


def sanitize_snippet(text: str | None, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    return _cap_normalized_snippet(_normalize_snippet(text), max_chars=max_chars)


def _cap_normalized_snippet(cleaned: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def _normalize_snippet(text: str | None) -> str:
    """Normalize group text without applying the persisted snippet cap."""
    if not text:
        return ""
    cleaned = "".join(ch if (ch.isprintable() or ch in "\r\n\t") else " " for ch in str(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_handle(value: Optional[str]) -> Optional[str]:
    """Normalize a Telegram @username: strip @/space, drop empties."""
    if not value:
        return None
    handle = str(value).strip().lstrip("@").strip()
    return handle or None


def _chat_key(value: Any) -> str:
    return str(value or "").strip().lstrip("@").lower()


def canonical_chat_peer(value: Any) -> str:
    """Stable Telegram peer spelling for storage, deduplication, and sending."""
    text = str(value or "").strip()
    if not text:
        return ""
    bare = text.lstrip("@")
    if text.startswith("@") or any(char.isalpha() or char == "_" for char in bare):
        return "@" + bare.lower()
    return text


def allowed_chats(st: Optional[dict] = None) -> list[str]:
    """All chats the telegram group stack serves: env/state primary mentions
    chat first, then the optional state list ``telegram_engage_chats``.
    Deduped, order-stable. THE single source of truth — the engage loop, the
    live-event allowlist, the backfill reader and the reader fallback must
    all resolve chats here (live incident 2026-07-09: three divergent
    resolutions let the engage loop serve a retired chat).

    Pass ``st`` when the caller already holds a loaded state dict; otherwise
    the state is loaded here."""
    chats: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = canonical_chat_peer(value)
        if not text:
            return
        key = _chat_key(text)
        if key and key not in seen:
            seen.add(key)
            chats.append(text)

    _add(os.environ.get("TELEGRAM_MENTIONS_CHAT", ""))
    try:
        if st is None:
            from .hooks import load_state
            st = load_state() or {}
        _add(st.get("telegram_mentions_chat"))
        extra = st.get("telegram_engage_chats")
        if isinstance(extra, list):
            for item in extra[:8]:
                _add(item)
    except Exception:
        pass
    # No default chat: an unconfigured inbox must stay silent. The old
    # hardcoded fallback kept re-attaching readers to a retired chat
    # (live incident 2026-07-09: cross-chat ghost replies).
    return chats


def allowed_chat() -> str:
    chats = allowed_chats()
    return chats[0] if chats else ""


def chat_matches(chat_username: Optional[str], chat_id: Any, allowed: str) -> bool:
    want = str(allowed or "").strip().lstrip("@").lower()
    if not want:
        return False
    if chat_username and str(chat_username).strip().lstrip("@").lower() == want:
        return True
    return str(chat_id) == want or str(chat_id).lstrip("-") == want.lstrip("-")


def chat_matches_any(chat_username: Optional[str], chat_id: Any,
                     allowed: list[str]) -> Optional[str]:
    """Canonical allowed-chat value this event belongs to, or None."""
    for candidate in allowed or []:
        if chat_matches(chat_username, chat_id, candidate):
            return candidate
    return None


class GroupInbox:
    """Bounded jsonl spool of group candidates + legacy receipt refresh."""

    def __init__(self, drive_root: Any):
        self._root = Path(drive_root)
        self._spool = self._root / SPOOL_REL
        self._receipt = self._root / RECEIPT_REL
        self._seq_path = self._root / SPOOL_SEQ_REL
        self._storage_lock_path = self._spool.parent / ".telegram_group_inbox.lock"
        self._lock = threading.Lock()
        self._seen: set[tuple[str, int]] = set()
        self._seen_order: deque[tuple[str, int]] = deque(maxlen=MAX_SEEN_IDS)
        self._own_ids: deque[tuple[str, int]] = deque(maxlen=MAX_OWN_IDS)
        self._load_seen_from_spool()

    def remember_own_message(self, chat: Any, message_id: int) -> None:
        with self._lock:
            self._own_ids.append((_chat_key(chat), int(message_id)))

    def add_message(self, *, chat: str, message_id: int, sender_id: Optional[int],
                    text: str, reply_to_msg_id: Optional[int], self_id: Optional[int],
                    sender_username: Optional[str] = None, sender_name: Optional[str] = None,
                    topic_id: Optional[int] = None, now: Optional[float] = None,
                    addressed_hint: bool = False,
                    matched_terms_hint: Optional[list[Any]] = None,
                    full_text_complete: Optional[bool] = None,
                    original_chars_hint: Optional[int] = None) -> bool:
        """Spool one group message. Returns True when a row was written."""
        return self.ingest_message(
            chat=chat,
            message_id=message_id,
            sender_id=sender_id,
            text=text,
            reply_to_msg_id=reply_to_msg_id,
            self_id=self_id,
            sender_username=sender_username,
            sender_name=sender_name,
            topic_id=topic_id,
            now=now,
            addressed_hint=addressed_hint,
            matched_terms_hint=matched_terms_hint,
            full_text_complete=full_text_complete,
            original_chars_hint=original_chars_hint,
        ).written

    def ingest_message(self, *, chat: str, message_id: int, sender_id: Optional[int],
                       text: str, reply_to_msg_id: Optional[int], self_id: Optional[int],
                       sender_username: Optional[str] = None,
                       sender_name: Optional[str] = None,
                       topic_id: Optional[int] = None,
                       now: Optional[float] = None,
                       addressed_hint: bool = False,
                       matched_terms_hint: Optional[list[Any]] = None,
                       full_text_complete: Optional[bool] = None,
                       original_chars_hint: Optional[int] = None) -> InboxAddResult:
        """Spool one message and expose whether a transport may ACK it.

        The seen-id cache is updated only after the jsonl row has been flushed
        and fsynced. This preserves the boolean ``add_message`` API while
        letting cursor-based transports distinguish duplicates from failures.
        """
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return InboxAddResult(False, True, "invalid_message_id")
        if self_id is not None and sender_id == self_id:
            self.remember_own_message(chat, mid)
            return InboxAddResult(False, True, "own_message")
        key = (_chat_key(chat), mid)
        terms = matched_terms(text)
        seen_terms = {term.casefold() for term in terms}
        if isinstance(matched_terms_hint, list):
            for raw_term in matched_terms_hint[:16]:
                term = sanitize_snippet(str(raw_term or ""), 80)
                if term and term.casefold() not in seen_terms:
                    terms.append(term)
                    seen_terms.add(term.casefold())
        from .hooks import redact
        redacted_text = redact(text)
        redacted_sender_username = redact(sender_username or "")
        redacted_sender_name = redact(sender_name or "")
        persisted_terms = [sanitize_snippet(redact(term), 80) for term in terms]

        with self._lock:
            if key in self._seen:
                return InboxAddResult(False, True, "duplicate")
            own_ids = set(self._own_ids)
            reply_to_me = (
                reply_to_msg_id is not None
                and (_chat_key(chat), int(reply_to_msg_id)) in own_ids
            )
            addressed = bool(terms) or reply_to_me or bool(addressed_hint)
            normalized_text = _normalize_snippet(redacted_text)
            snippet = _cap_normalized_snippet(normalized_text)
            truncated = normalized_text != snippet
            try:
                original_chars = max(
                    len(normalized_text),
                    min(10_000_000, int(original_chars_hint)),
                )
            except (TypeError, ValueError):
                original_chars = len(normalized_text)
            row = {
                "ts": time.time() if now is None else float(now),
                "chat": str(chat),
                "message_id": mid,
                "sender_id": sender_id,
                "sender_username": clean_handle(redacted_sender_username),
                "sender_name": (sanitize_snippet(redacted_sender_name, 64) or None),
                "reply_to_msg_id": (
                    int(reply_to_msg_id) if reply_to_msg_id is not None else None
                ),
                "topic_id": (int(topic_id) if topic_id is not None else None),
                "addressed": addressed,
                "matched_terms": persisted_terms + (["reply_to_me"] if reply_to_me else []),
                "snippet": snippet,
                "truncated": truncated,
                "original_chars": original_chars,
                "untrusted_external_text": True,
            }
            if addressed and truncated:
                # Progressive disclosure: the light decider sees only
                # ``snippet``; the deep composer can recover the explicit
                # question without retaining full unaddressed group chatter.
                row["full_text"] = normalized_text[:MAX_ADDRESSED_FULL_TEXT_CHARS]
                row["full_text_complete"] = (
                    len(normalized_text) <= MAX_ADDRESSED_FULL_TEXT_CHARS
                    and full_text_complete is not False
                )
            try:
                appended = self._append(row)
            except Exception:
                log.warning("telegram_group_inbox: spool append failed", exc_info=True)
                return InboxAddResult(False, False, "storage_failure")
            self._remember_seen_unlocked(key)
            if not appended:
                return InboxAddResult(False, True, "duplicate")
        if addressed:
            self._refresh_receipt(str(chat))
        try:
            from .roster import observe_message
            observe_message(self._root, chat, sender_id,
                            row["sender_username"], row["sender_name"],
                            now=row["ts"])
        except Exception:
            log.debug("telegram_group_inbox: roster observe failed", exc_info=True)
        return InboxAddResult(True, True, "written")

    def snapshot_seq(self) -> int:
        """Highest sequence durably present in the spool at one locked instant."""
        try:
            with self._lock:
                with self._storage_locked():
                    watermark = 0
                    ordinal = 0
                    with self._spool.open("r", encoding="utf-8") as spool_handle:
                        for line in spool_handle:
                            try:
                                row = json.loads(line)
                            except Exception:
                                continue
                            if not isinstance(row, dict):
                                continue
                            ordinal += 1
                            try:
                                watermark = max(
                                    watermark, int(row.get("spool_seq") or ordinal),
                                )
                            except (TypeError, ValueError):
                                watermark = max(watermark, ordinal)
                    return watermark
        except FileNotFoundError:
            return 0
        except Exception:
            log.debug("telegram_group_inbox: snapshot watermark failed", exc_info=True)
        return 0

    def pending(
        self,
        after_ts: float = 0.0,
        limit: int = 50,
        *,
        after_seq: Optional[int] = None,
        chat: Any = None,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a bounded spool view.

        ``chat`` is applied before ``limit`` so traffic in one group cannot
        evict another group's unread rows from every fetch.  UI/history callers
        keep the legacy newest-first window by default; queue consumers opt
        into ``oldest_first`` so advancing their cursor cannot skip backlog.
        """
        rows: list[dict[str, Any]] = []
        want_chat = _chat_key(chat) if chat is not None else None
        try:
            ordinal = 0
            with self._spool.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    ordinal += 1
                    try:
                        spool_seq = int(row.get("spool_seq") or ordinal)
                    except (TypeError, ValueError):
                        spool_seq = ordinal
                    row["spool_seq"] = spool_seq
                    if after_seq is not None:
                        if spool_seq <= int(after_seq):
                            continue
                    elif float(row.get("ts") or 0) <= after_ts:
                        continue
                    if want_chat is not None and _chat_key(row.get("chat")) != want_chat:
                        continue
                    rows.append(row)
        except FileNotFoundError:
            return []
        except Exception:
            log.warning("telegram_group_inbox: spool read failed", exc_info=True)
            return []
        try:
            bounded = max(0, int(limit))
        except (TypeError, ValueError):
            bounded = 50
        if bounded == 0:
            return []
        return rows[:bounded] if oldest_first else rows[-bounded:]

    def has_unconsumed_addressed(
        self, after_ts: float = 0.0, *, after_seq: Optional[int] = None,
        chat: Any = None,
    ) -> bool:
        return any(
            r.get("addressed")
            for r in self.pending(after_ts=after_ts, after_seq=after_seq, chat=chat)
        )

    # -- internals --

    def _remember_seen_unlocked(self, key: tuple[str, int]) -> None:
        if key in self._seen:
            return
        if len(self._seen_order) == self._seen_order.maxlen and self._seen_order:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(key)
        self._seen.add(key)

    def _load_seen_from_spool(self) -> None:
        """Hydrate recent durable ids so a replay after restart is a no-op."""
        try:
            with self._spool.open("r", encoding="utf-8") as source:
                lines = deque(source, maxlen=MAX_SEEN_IDS)
        except FileNotFoundError:
            return
        except Exception:
            log.warning("telegram_group_inbox: seen-id recovery failed", exc_info=True)
            return
        for line in lines:
            try:
                row = json.loads(line)
                key = (_chat_key(row["chat"]), int(row["message_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            self._remember_seen_unlocked(key)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":  # directory fsync is not portable
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _ensure_state_directory(self) -> None:
        directory = self._spool.parent
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            self._fsync_directory(directory.parent)

    @contextmanager
    def _storage_locked(self):
        self._ensure_state_directory()
        descriptor = os.open(
            self._storage_lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _repair_torn_tail_locked(self) -> None:
        """Drop an incomplete final jsonl row before appending another one."""
        try:
            with self._spool.open("r+b") as spool:
                payload = spool.read()
                if not payload or payload.endswith(b"\n"):
                    return
                row_start = payload.rfind(b"\n") + 1
                tail = payload[row_start:]
                complete = False
                try:
                    row = json.loads(tail.decode("utf-8"))
                    complete = isinstance(row, dict)
                    str(row["chat"])
                    int(row["message_id"])
                except (KeyError, TypeError, ValueError, UnicodeDecodeError,
                        json.JSONDecodeError):
                    complete = False
                spool.seek(0, os.SEEK_END)
                if complete:
                    spool.write(b"\n")
                else:
                    spool.truncate(row_start)
                spool.flush()
                os.fsync(spool.fileno())
        except FileNotFoundError:
            return

    def _spool_contains_key_locked(self, key: tuple[str, int]) -> bool:
        try:
            with self._spool.open("r", encoding="utf-8") as source:
                for line in source:
                    try:
                        row = json.loads(line)
                        candidate = (_chat_key(row["chat"]), int(row["message_id"]))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if candidate == key:
                        return True
        except FileNotFoundError:
            return False
        return False

    def _next_seq_locked(self) -> int:
        """Reserve the next monotonic spool sequence (sidecar + fsync)."""
        last_seq = 0
        try:
            last_seq = int(self._seq_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, TypeError, ValueError):
            try:
                ordinal = 0
                with self._spool.open("r", encoding="utf-8") as existing:
                    for line in existing:
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(item, dict):
                            continue
                        ordinal += 1
                        try:
                            last_seq = max(
                                last_seq, int(item.get("spool_seq") or ordinal),
                            )
                        except (TypeError, ValueError):
                            last_seq = max(last_seq, ordinal)
            except FileNotFoundError:
                pass
        next_seq = last_seq + 1
        seq_tmp = self._seq_path.with_name(
            f".{self._seq_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        with seq_tmp.open("w", encoding="utf-8") as seq_handle:
            seq_handle.write(f"{next_seq}\n")
            seq_handle.flush()
            os.fsync(seq_handle.fileno())
        os.replace(seq_tmp, self._seq_path)
        return next_seq

    def _rotate_locked(self, incoming_bytes: int = 0) -> None:
        try:
            if self._spool.stat().st_size + incoming_bytes <= MAX_SPOOL_BYTES:
                return
        except FileNotFoundError:
            return
        # Bound by encoded bytes, not row count: long addressed rows carry
        # ``full_text`` and 500 of them can exceed the byte cap on their own.
        budget = max(0, MAX_SPOOL_BYTES - incoming_bytes)
        lines = self._spool.read_bytes().splitlines(keepends=True)
        kept_newest: list[bytes] = []
        kept_bytes = 0
        for raw_line in reversed(lines[-MAX_SPOOL_RETAIN_ROWS:]):
            line = raw_line if raw_line.endswith(b"\n") else raw_line + b"\n"
            if kept_bytes + len(line) > budget:
                break
            kept_newest.append(line)
            kept_bytes += len(line)
        payload = b"".join(reversed(kept_newest))
        temporary_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._spool.parent,
                prefix=".telegram-inbox-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary_name, 0o600)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self._spool)
            temporary_name = None
            self._fsync_directory(self._spool.parent)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def _append(self, row: dict[str, Any]) -> bool:
        """Append one private durable row; return False for an on-disk duplicate."""
        key = (_chat_key(row["chat"]), int(row["message_id"]))
        with self._storage_locked():
            self._repair_torn_tail_locked()
            if self._spool_contains_key_locked(key):
                # Confirm the directory entry before allowing a replayed update
                # to advance the upstream cursor.
                self._fsync_directory(self._spool.parent)
                return False
            row["spool_seq"] = self._next_seq_locked()
            encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
            self._rotate_locked(len(encoded))
            descriptor = os.open(
                self._spool,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                except OSError:
                    pass
                with os.fdopen(descriptor, "wb", closefd=False) as spool:
                    spool.write(encoded)
                    spool.flush()
                    os.fsync(spool.fileno())
            finally:
                os.close(descriptor)
            self._fsync_directory(self._spool.parent)
            return True

    def _refresh_receipt(self, chat: str) -> None:
        """Keep the legacy monitor receipt alive for dashboards/contracts."""
        try:
            addressed = [r["message_id"] for r in self.pending(after_ts=0.0, limit=200)
                         if r.get("addressed")]
            payload = {
                "status": "new_addressed_signal",
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "chat": chat,
                "source": "telegram_presence.inbox (realtime)",
                "addressed_ids": addressed[-50:],
                "handled_message_ids": addressed[-50:],
                "new_addressed_signal": True,
                "no_new_addressed_signal": False,
                "blocker": None,
            }
            self._receipt.parent.mkdir(parents=True, exist_ok=True)
            self._receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        except Exception:
            log.warning("telegram_group_inbox: receipt refresh failed", exc_info=True)


_INBOX: Optional[GroupInbox] = None
_INBOX_LOCK = threading.Lock()


def get_inbox(drive_root: Any = None) -> Optional[GroupInbox]:
    global _INBOX
    with _INBOX_LOCK:
        if _INBOX is None and drive_root is not None:
            _INBOX = GroupInbox(drive_root)
        return _INBOX
