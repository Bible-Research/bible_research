"""REST client for the Gemini ``generateContent`` TTS endpoint.

Calls ``generativelanguage.googleapis.com`` directly with
``requests`` (no ``google-genai`` dependency) so the free-tier
``gemini-2.5-flash-preview-tts`` model can synthesize chapter audio.
API keys come from ``config.yaml`` via ``settings.GEMINI_API_KEYS``;
the key travels in the ``x-goog-api-key`` header so it never lands
in a URL, log line, or error message.
"""
import base64
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GENERATE_CONTENT_URL = (
    "https://generativelanguage.googleapis.com"
    "/v1beta/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE_NAME = "Sadachbia"
# Generation of a ~10-minute audio response can take well over a
# minute server-side; keep the read timeout generous.
DEFAULT_TIMEOUT_SECONDS = 240


class QuotaExceeded(Exception):
    """Every configured API key returned HTTP 429 — stop for the day."""


class GeminiApiError(Exception):
    """Non-quota HTTP or transport failure from the Gemini API."""


class GeminiResponseError(Exception):
    """The API answered 200 but returned no audio payload."""


@dataclass
class SynthesisResult:
    """One successful TTS call: raw LINEAR16 PCM plus token usage."""
    pcm_bytes: bytes
    output_tokens: Optional[int]


class GeminiTTSClient:
    """Multi-key Gemini TTS client with per-request pacing.

    ``api_keys`` holds one key per free-tier GCP project; when a key
    is rejected (429 quota exhaustion, or a 403/400 invalid-key
    response) the client rotates to the next key for the remainder
    of the run and only raises :class:`QuotaExceeded` once every
    key is exhausted.
    """

    def __init__(
        self,
        api_keys: List[str],
        model: str = DEFAULT_MODEL,
        voice_name: str = DEFAULT_VOICE_NAME,
        min_interval_seconds: float = 0.0,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self._keys = [k for k in api_keys if k]
        self._model = model
        self._voice_name = voice_name
        self._min_interval = min_interval_seconds
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._key_index = 0
        self._exhausted: set = set()
        self._last_request_at: Optional[float] = None
        # Count of HTTP requests actually sent — retries included —
        # so callers can persist true daily-quota spend.
        self.requests_made = 0

    def synthesize(
        self,
        text: str,
        voice_name: Optional[str] = None,
    ) -> SynthesisResult:
        """Synthesize ``text`` to raw LINEAR16 24 kHz mono PCM.

        Raises :class:`QuotaExceeded` when every key is
        rate-limited or rejected, :class:`GeminiApiError` on other
        API/transport failures, and :class:`GeminiResponseError`
        when the response carries no audio part."""
        if not self._keys:
            raise GeminiApiError(
                "No Gemini API keys configured; add GEMINI_API_KEY "
                "to config.yaml."
            )
        payload = build_payload(
            text, voice_name or self._voice_name,
        )
        while True:
            key = self._current_key()
            if key is None:
                raise QuotaExceeded(
                    "All Gemini API keys were rejected or hit the "
                    "daily quota; the free quota resets at "
                    "midnight Pacific."
                )
            status, body = self._post(key, payload)
            if _is_key_rejection(status, body):
                # Key-scoped failure (quota spent, or the key is
                # invalid/revoked): burn the key and try the next
                # one. 5xx and transport errors stay fatal below.
                self._exhausted.add(key)
                logger.warning(
                    "Gemini API key #%d failed (HTTP %d); "
                    "rotating to the next key.",
                    self._key_index + 1, status,
                )
                continue
            if status == 200:
                return parse_tts_response(body)
            raise GeminiApiError(
                f"Gemini generateContent failed: HTTP {status}"
            )

    def _current_key(self) -> Optional[str]:
        """First key that has not yet failed this run."""
        while self._key_index < len(self._keys):
            key = self._keys[self._key_index]
            if key not in self._exhausted:
                return key
            self._key_index += 1
        return None

    def _post(self, key: str, payload: dict):
        """Send one ``generateContent`` request, paced to
        ``min_interval_seconds`` since the previous request."""
        if self._min_interval > 0 and (
            self._last_request_at is not None
        ):
            wait = (
                self._min_interval
                - (self._monotonic() - self._last_request_at)
            )
            if wait > 0:
                self._sleep(wait)
        url = GENERATE_CONTENT_URL.format(model=self._model)
        try:
            response = self._session.post(
                url,
                json=payload,
                headers={"x-goog-api-key": key},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise GeminiApiError(
                f"Gemini request failed: {type(exc).__name__}"
            ) from exc
        finally:
            self._last_request_at = self._monotonic()
            self.requests_made += 1
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body


def _is_key_rejection(status: int, body: dict) -> bool:
    """Whether an HTTP failure is scoped to the API key.

    429 means the key's quota is spent and 403 means the key (or
    its project) is forbidden — both warrant rotation. A 400 only
    rotates when the error actually names the API key; a generic
    bad-request 400 would fail identically on every key, so it
    stays a fatal :class:`GeminiApiError`."""
    if status in (403, 429):
        return True
    if status != 400:
        return False
    error = body.get("error") or {}
    details = error.get("details") or []
    reasons = {
        d.get("reason", "")
        for d in details
        if isinstance(d, dict)
    }
    if "API_KEY_INVALID" in reasons:
        return True
    return "api key" in str(error.get("message", "")).lower()


def build_payload(text: str, voice_name: str) -> dict:
    """Build the ``generateContent`` request body for TTS output."""
    return {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice_name,
                    },
                },
            },
        },
    }


def parse_tts_response(body: dict) -> SynthesisResult:
    """Extract audio bytes and the output token count.

    The audio lives in ``candidates[].content.parts[].inlineData``
    as base64 LINEAR16 PCM. ``usageMetadata.candidatesTokenCount``
    counts generated audio tokens — the truncation signal, since
    ``finishReason`` stays ``STOP`` even on truncated output."""
    candidates = body.get("candidates") or []
    pcm_bytes = b""
    for candidate in candidates:
        parts = (
            candidate.get("content", {}).get("parts", [])
        )
        for part in parts:
            inline = part.get("inlineData") or part.get(
                "inline_data"
            )
            if inline and inline.get("data"):
                pcm_bytes += base64.b64decode(inline["data"])
    if not pcm_bytes:
        raise GeminiResponseError(
            "Gemini response contained no audio part "
            f"(finishReasons="
            f"{[c.get('finishReason') for c in candidates]})"
        )

    usage = body.get("usageMetadata") or {}
    output_tokens = (
        usage.get("candidatesTokenCount")
        or usage.get("completionTokenCount")
    )
    return SynthesisResult(
        pcm_bytes=pcm_bytes,
        output_tokens=output_tokens,
    )


_default: Optional[GeminiTTSClient] = None
_default_lock = threading.Lock()


def get_default_client() -> GeminiTTSClient:
    """Process-wide client built from ``settings.GEMINI_API_KEYS``."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = GeminiTTSClient(
                    api_keys=list(
                        getattr(settings, "GEMINI_API_KEYS", [])
                    ),
                )
    return _default
