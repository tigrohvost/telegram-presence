"""Integration hooks: everything host-specific is injected here.

The engage/inbox/delegate modules were extracted from a live agent
(Rain / the Ouroboros project). Inside that agent they reached into her
state store, persona voice card and outward-text sanitizer. As a
standalone package those touch points become five injectable hooks with
safe defaults, so the package works out of the box and a host agent can
plug its own organs in one `configure()` call:

    from telegram_presence import hooks
    hooks.configure(
        agent_name="Rain",
        name_terms=("rain", "рейн"),
        state_loader=my_state_loader,          # () -> dict
        voice_card_loader=my_voice_loader,     # (drive_root) -> str
        outward_sanitizer=my_gate,             # (str) -> str
        redactor=my_redactor,                  # (str) -> str
    )
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Default addressed-detection terms; override via configure(name_terms=...).
DEFAULT_NAME_TERMS = ("rain", "rain_ouroboros", "рейн", "рэйн", "ouroboros", "ороборос")

# Secret-like token patterns for the default redactor. No word-boundary
# anchors on the prefixes: json.dumps renders a real newline as a literal
# backslash-n, gluing the previous character to the token so \b never fires.
_SECRET_RES = [
    re.compile(r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{10,}"),
    re.compile(r"am_[A-Za-z0-9]{2}_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),           # long base64/hex blobs
    re.compile(r"(?i)(password|token|secret|api[_-]?key|bearer)[=:\s]+\S{6,}"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),                  # creds in URLs
    re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+:\S{6,}"),          # basic-auth email:password
    re.compile(r"(?i)\b\w*(?:pass|pwd)\w*\s*=\s*[\"']?\S{6,}"),  # FOO_PASS='...'
]


def _default_redact(text: str) -> str:
    value = str(text or "")
    for rx in _SECRET_RES:
        value = rx.sub("«redacted»", value)
    return value


# Leaked-reasoning header at the start of a reply ("**Thinking**\n...\n\n"):
# strip it through the first blank line before the text goes outward.
_THINKING_HEAD = re.compile(r"^\s*\*\*[^\n*]{1,60}\*\*[^\n]*\n.*?(?:\n\s*\n|\Z)", re.S)


def _default_outward_sanitize(text: str) -> str:
    value = str(text or "")
    value = _THINKING_HEAD.sub("", value, count=1)
    return value.strip()


_agent_name: str = "the agent"
_name_terms: tuple = DEFAULT_NAME_TERMS
_state_loader: Callable[[], dict] = lambda: {}
_voice_card_loader: Callable[[Any], str] = lambda drive_root: ""
_outward_sanitizer: Callable[[str], str] = _default_outward_sanitize
_redactor: Callable[[str], str] = _default_redact


def configure(
    *,
    agent_name: Optional[str] = None,
    name_terms: Optional[tuple] = None,
    state_loader: Optional[Callable[[], dict]] = None,
    voice_card_loader: Optional[Callable[[Any], str]] = None,
    outward_sanitizer: Optional[Callable[[str], str]] = None,
    redactor: Optional[Callable[[str], str]] = None,
) -> None:
    """Install host-agent integrations. Call once at startup; every argument
    is optional and unset arguments keep their current value."""
    global _agent_name, _name_terms, _state_loader
    global _voice_card_loader, _outward_sanitizer, _redactor
    if agent_name is not None:
        _agent_name = str(agent_name)
    if name_terms is not None:
        _name_terms = tuple(name_terms)
    if state_loader is not None:
        _state_loader = state_loader
    if voice_card_loader is not None:
        _voice_card_loader = voice_card_loader
    if outward_sanitizer is not None:
        _outward_sanitizer = outward_sanitizer
    if redactor is not None:
        _redactor = redactor


def agent_name() -> str:
    return _agent_name


def name_terms() -> tuple:
    return _name_terms


def load_state() -> dict:
    """Host state dict (chat pauses, engage config). Never raises."""
    try:
        return _state_loader() or {}
    except Exception:
        return {}


def load_voice_card(drive_root: Any) -> str:
    """Persona voice card text prepended to the decider prompt."""
    try:
        return str(_voice_card_loader(drive_root) or "")
    except Exception:
        return ""


def sanitize_outward(text: str) -> str:
    """Host gate for outward-facing text (leak/markup stripping)."""
    try:
        return str(_outward_sanitizer(str(text or "")))
    except Exception:
        return str(text or "")


def redact(text: str) -> str:
    """Secret-like token redaction for spooled snippets and logs."""
    try:
        return str(_redactor(str(text or "")))
    except Exception:
        return _default_redact(text)
