"""Request/response tests for ``GeminiTTSClient``.

All HTTP is faked through an injected ``requests.Session`` — the
tests pin the generateContent payload shape, the
``x-goog-api-key`` auth header (key must never appear in the URL),
429 key rotation, and the usage-metadata parsing the truncation
detector relies on. No test may hit the real API.
"""
import base64
import json

import pytest

from bible.services.gemini_tts.client import (
    GeminiApiError,
    GeminiResponseError,
    GeminiTTSClient,
    QuotaExceeded,
)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeSession:
    """Queue of canned responses; records every ``post`` call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({
            "url": url, "json": json,
            "headers": headers, "timeout": timeout,
        })
        return self._responses.pop(0)


def _tts_payload(pcm=b"\x01\x02" * 100, output_tokens=100):
    return {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{
                "inlineData": {
                    "mimeType": "audio/L16;codec=pcm;rate=24000",
                    "data": base64.b64encode(pcm).decode(),
                },
            }]},
        }],
        "usageMetadata": {
            "promptTokenCount": 50,
            "candidatesTokenCount": output_tokens,
        },
    }


def _client(session, keys=("key-a",), **kwargs):
    return GeminiTTSClient(
        api_keys=list(keys), session=session, **kwargs,
    )


def test_synthesize_posts_generate_content_with_key_header():
    session = _FakeSession([_FakeResponse(200, _tts_payload())])
    client = _client(session)

    result = client.synthesize(text="Sveiki pasaule")

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith(
        "/v1beta/models/"
        "gemini-2.5-flash-preview-tts:generateContent"
    )
    # The key authenticates via header only — never in the URL.
    assert call["headers"] == {"x-goog-api-key": "key-a"}
    assert "key-a" not in call["url"]
    body = call["json"]
    assert body["contents"][0]["parts"][0]["text"] == (
        "Sveiki pasaule"
    )
    gen_cfg = body["generationConfig"]
    assert gen_cfg["responseModalities"] == ["AUDIO"]
    voice = gen_cfg["speechConfig"]["voiceConfig"]
    assert voice["prebuiltVoiceConfig"]["voiceName"] == "Sadachbia"
    assert result.pcm_bytes == b"\x01\x02" * 100
    assert result.output_tokens == 100
    assert client.requests_made == 1


def test_429_rotates_to_next_api_key():
    session = _FakeSession([
        _FakeResponse(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}),
        _FakeResponse(200, _tts_payload()),
    ])
    client = _client(session, keys=("key-a", "key-b"))

    result = client.synthesize(text="teksts")

    assert result.pcm_bytes
    used_keys = [
        c["headers"]["x-goog-api-key"] for c in session.calls
    ]
    assert used_keys == ["key-a", "key-b"]
    assert client.requests_made == 2


def test_quota_exceeded_when_every_key_returns_429():
    session = _FakeSession([
        _FakeResponse(429, {}),
        _FakeResponse(429, {}),
    ])
    client = _client(session, keys=("key-a", "key-b"))

    with pytest.raises(QuotaExceeded):
        client.synthesize(text="teksts")
    assert client.requests_made == 2


def test_exhausted_key_stays_exhausted_across_calls():
    """A key that 429s must not be retried on later requests."""
    session = _FakeSession([
        _FakeResponse(429, {}),   # key-a dies here
        _FakeResponse(200, _tts_payload()),
        _FakeResponse(200, _tts_payload()),
    ])
    client = _client(session, keys=("key-a", "key-b"))

    client.synthesize(text="viens")
    client.synthesize(text="divi")

    used_keys = [
        c["headers"]["x-goog-api-key"] for c in session.calls
    ]
    assert used_keys == ["key-a", "key-b", "key-b"]


def test_non_429_http_error_raises_api_error():
    session = _FakeSession([
        _FakeResponse(500, {"error": {"message": "boom"}}),
    ])
    client = _client(session)

    with pytest.raises(GeminiApiError, match="HTTP 500"):
        client.synthesize(text="teksts")


def test_response_without_audio_part_raises_response_error():
    session = _FakeSession([
        _FakeResponse(200, {
            "candidates": [{"finishReason": "STOP"}],
        }),
    ])
    client = _client(session)

    with pytest.raises(GeminiResponseError):
        client.synthesize(text="teksts")


def test_no_configured_keys_raises_api_error():
    client = GeminiTTSClient(api_keys=[], session=_FakeSession([]))

    with pytest.raises(GeminiApiError, match="No Gemini API keys"):
        client.synthesize(text="teksts")


def test_min_interval_pacing_sleeps_between_requests():
    """3 RPM pacing: the second request must wait out the rest of
    the interval measured since the first request."""
    sleeps = []
    now = [1000.0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    session = _FakeSession([
        _FakeResponse(200, _tts_payload()),
        _FakeResponse(200, _tts_payload()),
    ])
    client = _client(
        session,
        min_interval_seconds=21.0,
        sleep_fn=fake_sleep,
        monotonic_fn=lambda: now[0],
    )

    client.synthesize(text="viens")
    # First request is immediate; simulate 4 s of elapsed work.
    now[0] += 4.0
    client.synthesize(text="divi")

    assert sleeps == [17.0]
    assert client.requests_made == 2
