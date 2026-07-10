import pytest

from telegram_presence.content import (
    DEFAULT_MEDIA_LIMITS,
    MediaDescriptor,
    semantic_chunks,
    validate_media,
)


def test_semantic_chunks_prefer_sentences_and_keep_all_words():
    text = "Первое короткое предложение. Второе предложение подлиннее!\n\nНовый абзац."
    chunks = semantic_chunks(text, max_chars=42)
    assert chunks == [
        "Первое короткое предложение.",
        "Второе предложение подлиннее!",
        "Новый абзац.",
    ]
    assert all(len(chunk) <= 42 for chunk in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_semantic_chunks_hard_split_only_overlong_token():
    chunks = semantic_chunks("abcdefghij", max_chars=4)
    assert chunks == ["abcd", "efgh", "ij"]
    assert semantic_chunks("  ") == []
    with pytest.raises(ValueError):
        semantic_chunks("text", max_chars=0)
    with pytest.raises(ValueError, match="maximum"):
        semantic_chunks("один два три четыре пять", max_chars=5, max_chunks=2)


def test_media_mime_and_size_are_bounded_before_io():
    descriptor = MediaDescriptor("IMAGE/PNG; charset=binary", 100, "blob:key")
    assert descriptor.mime_type == "image/png"
    validate_media("image/png", DEFAULT_MEDIA_LIMITS["image/"])
    with pytest.raises(ValueError, match="not allowed"):
        validate_media("application/x-executable", 100)
    with pytest.raises(ValueError, match="exceeds"):
        validate_media("image/png", DEFAULT_MEDIA_LIMITS["image/"] + 1)
    with pytest.raises(ValueError, match="positive"):
        validate_media("image/png", 0)
    with pytest.raises(TypeError):
        DEFAULT_MEDIA_LIMITS["image/"] = 999999999
    with pytest.raises(ValueError, match="safe basename"):
        MediaDescriptor("image/png", 100, "blob:key", "../escape.png")
