"""Safe outbound content helpers shared by Telegram transports.

The limits in this module are deliberately conservative host-side bounds.
They are validated before transport I/O and can be tightened by a caller.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Collection, Optional


TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_MAX_TEXT_CHUNKS = 8
MIB = 1024 * 1024

DEFAULT_ALLOWED_MEDIA_MIME_TYPES = frozenset({
    "application/json",
    "application/pdf",
    "application/zip",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
    "video/mp4",
})

DEFAULT_MEDIA_LIMITS = MappingProxyType({
    "application/": 20 * MIB,
    "audio/": 20 * MIB,
    "image/": 10 * MIB,
    "text/": 1 * MIB,
    "video/": 20 * MIB,
})

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n+")


def _split_long_unit(text: str, limit: int) -> list[str]:
    """Split an over-limit sentence on words, hard-splitting only a word
    that cannot fit by itself."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current = ""
    for word in words:
        while len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def semantic_chunks(
    text: str,
    max_chars: int = TELEGRAM_TEXT_LIMIT,
    *,
    max_chunks: Optional[int] = DEFAULT_MAX_TEXT_CHUNKS,
) -> list[str]:
    """Split text without silently truncating it.

    Paragraphs are preferred over sentences, sentences over words, and a
    hard character split is used only for a single over-limit token. Whitespace
    at split boundaries is normalized; the words and their order are kept.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if (max_chunks is not None
            and (isinstance(max_chunks, bool) or not isinstance(max_chunks, int)
                 or max_chunks < 1)):
        raise ValueError("max_chunks must be a positive integer or None")

    value = str(text).strip()
    if not value:
        return []
    if len(value) <= max_chars:
        return [value]

    units: list[tuple[str, str]] = []
    for paragraph_index, paragraph in enumerate(_PARAGRAPH_BOUNDARY.split(value)):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(paragraph)
                     if part.strip()]
        for sentence_index, sentence in enumerate(sentences):
            separator = "\n\n" if paragraph_index and sentence_index == 0 else " "
            if not units:
                separator = ""
            parts = ([sentence] if len(sentence) <= max_chars
                     else _split_long_unit(sentence, max_chars))
            for part_index, part in enumerate(parts):
                units.append((part, separator if part_index == 0 else " "))

    chunks: list[str] = []
    current = ""
    for unit, separator in units:
        candidate = f"{current}{separator}{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = unit
    if current:
        chunks.append(current)
    if max_chunks is not None and len(chunks) > max_chunks:
        raise ValueError(f"text requires {len(chunks)} chunks; maximum is {max_chunks}")
    return chunks


def _normalized_mime(mime_type: str) -> str:
    if not isinstance(mime_type, str):
        raise ValueError("mime_type must be a string")
    value = mime_type.split(";", 1)[0].strip().lower()
    if not value or "/" not in value:
        raise ValueError("mime_type must be a concrete type/subtype")
    return value


def _default_media_limit(mime_type: str) -> int:
    for prefix, limit in DEFAULT_MEDIA_LIMITS.items():
        if mime_type.startswith(prefix):
            return limit
    raise ValueError(f"no size policy for media MIME type: {mime_type}")


def validate_media(
    mime_type: str,
    size_bytes: int,
    *,
    allowed_mime_types: Optional[Collection[str]] = None,
    max_size_bytes: Optional[int] = None,
) -> None:
    """Raise ``ValueError`` when a media descriptor violates policy."""
    normalized = _normalized_mime(mime_type)
    allowed = (DEFAULT_ALLOWED_MEDIA_MIME_TYPES if allowed_mime_types is None
               else frozenset(_normalized_mime(item) for item in allowed_mime_types))
    if normalized not in allowed:
        raise ValueError(f"media MIME type is not allowed: {normalized}")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise ValueError("size_bytes must be a positive integer")

    limit = _default_media_limit(normalized) if max_size_bytes is None else max_size_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("max_size_bytes must be a positive integer")
    if size_bytes > limit:
        raise ValueError(f"media exceeds limit: {size_bytes} > {limit} bytes")


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    """Serializable metadata for media referenced by an outbound envelope.

    ``source`` is a host-owned path or opaque key. The outbox stores only the
    descriptor, never the media bytes themselves.
    """

    mime_type: str
    size_bytes: int
    source: str
    filename: Optional[str] = None

    def __post_init__(self) -> None:
        validate_media(self.mime_type, self.size_bytes)
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty path or opaque key")
        if "\x00" in self.source or len(self.source) > 4096:
            raise ValueError("source contains unsafe path data")
        filename = self.filename
        if filename is not None:
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError("filename must be non-empty when provided")
            filename = filename.strip()
            if (len(filename) > 255 or "\x00" in filename
                    or "/" in filename or "\\" in filename
                    or filename in (".", "..")):
                raise ValueError("filename must be a safe basename")
        object.__setattr__(self, "mime_type", _normalized_mime(self.mime_type))
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "filename", filename)

    def to_dict(self) -> dict:
        return {
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "filename": self.filename,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "MediaDescriptor":
        return cls(
            mime_type=value["mime_type"],
            size_bytes=value["size_bytes"],
            source=value["source"],
            filename=value.get("filename"),
        )
