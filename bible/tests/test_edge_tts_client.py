"""Tests for ``EdgeTTSClient`` retry/backoff policy.

``_stream_once`` is patched with an ``AsyncMock`` — calling it
returns a coroutine ``asyncio.run`` can drive, so no real
edge-tts traffic happens.
"""
from unittest.mock import AsyncMock, patch

import pytest

from bible.services.edge_tts.client import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_BACKOFF_JITTER,
    RETRY_BACKOFF_MAX_SECONDS,
    RETRY_BACKOFF_SECONDS,
    EdgeSynthesisResult,
    EdgeTTSError,
    EdgeTTSClient,
)

_CLIENT = "bible.services.edge_tts.client"


def _result() -> EdgeSynthesisResult:
    mp3 = b"\xff\xfb" + b"\x00" * 100
    from bible.services.edge_tts.client import mp3_duration_seconds
    return EdgeSynthesisResult(
        mp3_bytes=mp3,
        duration_seconds=mp3_duration_seconds(mp3),
    )


def _client(sleeps, max_attempts=DEFAULT_MAX_ATTEMPTS, **kwargs):
    return EdgeTTSClient(
        max_attempts=max_attempts,
        sleep_fn=sleeps.append,
        **kwargs,
    )


def test_first_try_success_never_sleeps():
    sleeps = []
    stream = AsyncMock(return_value=_result())
    with patch(f"{_CLIENT}._stream_once", new=stream):
        client = _client(sleeps)
        assert client.synthesize("abc").mp3_bytes
    assert stream.call_count == 1
    assert client.requests_made == 1
    assert sleeps == []


def test_retries_then_succeeds_counts_every_attempt():
    sleeps = []
    stream = AsyncMock(
        side_effect=[Exception("no audio"), Exception("wss"),
                     _result()],
    )
    with patch(f"{_CLIENT}._stream_once", new=stream):
        client = _client(sleeps)
        assert client.synthesize("abc").mp3_bytes
    assert stream.call_count == 3
    assert client.requests_made == 3
    # Slept before attempt 2 and attempt 3 only.
    assert len(sleeps) == 2


def test_backoff_doubles_then_caps_at_max():
    """With jitter pinned to the midpoint the schedule is
    exactly base * 2**(n-1), capped."""
    sleeps = []
    stream = AsyncMock(side_effect=Exception("no audio"))

    def midpoint(lo, hi):
        return (lo + hi) / 2

    with patch(f"{_CLIENT}._stream_once", new=stream):
        client = _client(
            sleeps, max_attempts=6, random_fn=midpoint,
        )
        with pytest.raises(EdgeTTSError):
            client.synthesize("abc")
    assert sleeps == [
        min(RETRY_BACKOFF_SECONDS * 2 ** n,
            RETRY_BACKOFF_MAX_SECONDS)
        for n in range(5)
    ]
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 30.0]


def test_backoff_jitter_stays_within_bounds():
    """Real ``random.uniform`` jitter keeps every delay inside
    base ± ``RETRY_BACKOFF_JITTER``."""
    sleeps = []
    stream = AsyncMock(side_effect=Exception("no audio"))
    with patch(f"{_CLIENT}._stream_once", new=stream):
        client = _client(sleeps)
        with pytest.raises(EdgeTTSError):
            client.synthesize("abc")
    assert len(sleeps) == DEFAULT_MAX_ATTEMPTS - 1
    for n, delay in enumerate(sleeps):
        base = min(
            RETRY_BACKOFF_SECONDS * 2 ** n,
            RETRY_BACKOFF_MAX_SECONDS,
        )
        delta = base * RETRY_BACKOFF_JITTER
        assert base - delta <= delay <= base + delta


def test_exhaustion_raises_edge_tts_error():
    sleeps = []
    stream = AsyncMock(side_effect=Exception("no audio"))
    with patch(f"{_CLIENT}._stream_once", new=stream):
        client = _client(sleeps, max_attempts=3)
        with pytest.raises(
            EdgeTTSError, match="after 3 attempts"
        ):
            client.synthesize("abc")
    assert client.requests_made == 3
    assert stream.call_count == 3
    # No sleep after the final attempt.
    assert len(sleeps) == 2
