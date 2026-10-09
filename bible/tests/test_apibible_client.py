"""Tests for the API.Bible (NIV) provider.

All HTTP is mocked — no real ``API_BIBLE_KEY`` exists and tests
must never call the live ``api.scripture.api.bible`` host.
"""
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from bible.serializers import BiblePassageSerializer
from bible.services.apibible.client import (
    ApiBibleClient,
    _parse_chapter_content,
)
from bible.services.apibible.registry import (
    APIBIBLE_TRANSLATIONS,
    canonical_apibible_fileset_id,
    get_apibible_meta,
    get_apibible_translation_listing,
    is_apibible_fileset,
)
from bible.utils.provider_errors import (
    PassageNotFoundError,
    provider_error_fields,
)

User = get_user_model()

BIBLE_ID = APIBIBLE_TRANSLATIONS["ENGNIV_API"]["bible_id"]
AUDIO_BIBLE_ID = (
    APIBIBLE_TRANSLATIONS["ENGNIV_API"]["audio_bible_id"]
)

# Representative API.Bible ``data.content`` payload: a heading
# paragraph followed by a prose paragraph carrying verse
# sid/eid span markers, then a verse-span-style paragraph where
# verses appear as ``type: 'verse'`` items with ``verseId``.
_CHAPTER_CONTENT = [
    {
        "name": "para",
        "type": "tag",
        "attrs": {"style": "s1"},
        "items": [
            {"type": "text", "text": "Jesus Teaches Nicodemus"},
        ],
    },
    {
        "name": "para",
        "type": "tag",
        "attrs": {"style": "p"},
        "items": [
            {
                "name": "verse",
                "type": "tag",
                "attrs": {
                    "number": "1",
                    "sid": "JHN.3.1",
                    "style": "v",
                },
                "items": [{"type": "text", "text": "1"}],
            },
            {
                "type": "text",
                "text": (
                    "Now there was a Pharisee, a man named "
                    "Nicodemus who was a member of the Jewish "
                    "ruling council."
                ),
            },
            {
                "name": "verse",
                "type": "tag",
                "attrs": {"eid": "JHN.3.1"},
            },
            {
                "name": "verse",
                "type": "tag",
                "attrs": {
                    "number": "2",
                    "sid": "JHN.3.2",
                    "style": "v",
                },
                "items": [{"type": "text", "text": "2"}],
            },
            {
                "type": "text",
                "text": "He came to Jesus at night.",
            },
            {
                "name": "verse",
                "type": "tag",
                "attrs": {"eid": "JHN.3.2"},
            },
        ],
    },
    {
        "name": "para",
        "type": "tag",
        "attrs": {"style": "q1"},
        "items": [
            {"type": "verse", "verseId": "JHN.3.16"},
            {
                "type": "text",
                "text": (
                    "For God so loved the world that he gave "
                    "his one and only Son,"
                ),
            },
            {"type": "verse", "verseId": "JHN.3.17"},
            {
                "type": "text",
                "text": (
                    "For God did not send his Son into the "
                    "world to condemn the world."
                ),
            },
        ],
    },
]

_FUMS_META = {
    "fums": "<script>_BAPI.t('abc123')</script>",
    "fumsId": "abc123",
    "fumsJsInclude": (
        "https://cdn.scripture.api.bible/fums/fumsv2.min.js"
    ),
    "fumsNoScript": "<img src='https://d3btgtzu3ctdwx.cloudfront.net/x.png'>",
}


def _json_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _http_error(status_code):
    err = requests.HTTPError(f"HTTP {status_code}")
    err.response = MagicMock(status_code=status_code)
    return err


# ============================================================
# Registry helpers (must work with no API key configured)
# ============================================================

class ApiBibleRegistryTests(TestCase):
    def test_is_apibible_fileset_text(self):
        self.assertTrue(is_apibible_fileset("ENGNIV_API"))

    def test_is_apibible_fileset_audio(self):
        self.assertTrue(is_apibible_fileset("ENGNIVC1DA"))

    def test_is_apibible_fileset_case_insensitive(self):
        self.assertTrue(is_apibible_fileset("engniv_api"))

    def test_is_apibible_fileset_false_for_unknown(self):
        self.assertFalse(is_apibible_fileset("ENGNIV"))
        self.assertFalse(is_apibible_fileset("ENGESV_API"))
        self.assertFalse(is_apibible_fileset("LVSGLU8"))
        self.assertFalse(is_apibible_fileset(""))

    def test_audio_fileset_canonicalizes_to_text(self):
        self.assertEqual(
            canonical_apibible_fileset_id("ENGNIVC1DA"),
            "ENGNIV_API",
        )

    def test_get_apibible_meta(self):
        meta = get_apibible_meta("ENGNIVC1DA")
        self.assertEqual(meta["abbr"], "NIV")
        self.assertEqual(meta["audio_fileset_id"], "ENGNIVC1DA")

    def test_get_apibible_meta_unknown_raises(self):
        with self.assertRaises(KeyError):
            get_apibible_meta("ENGKJV")

    def test_translation_listing_shape(self):
        listing = get_apibible_translation_listing()
        self.assertEqual(len(listing), 1)
        entry = listing[0]
        self.assertEqual(entry["abbr"], "NIV")
        self.assertEqual(entry["iso"], "eng")
        filesets = {f["id"]: f for f in entry["filesets"]}
        self.assertEqual(
            filesets["ENGNIV_API"]["type"], "text_plain"
        )
        self.assertEqual(
            filesets["ENGNIVC1DA"]["type"], "audio"
        )


# ============================================================
# Chapter content parser
# ============================================================

class ParseChapterContentTests(TestCase):
    def test_extracts_verses_across_marker_shapes(self):
        result = _parse_chapter_content(_CHAPTER_CONTENT)
        verses = result["verses"]
        nums = [v["verse_start"] for v in verses]
        self.assertEqual(nums, [1, 2, 16, 17])
        v16 = verses[2]
        self.assertIn("For God so loved", v16["verse_text"])
        # The printed verse-number label must not leak into
        # verse text.
        v1 = verses[0]
        self.assertTrue(
            v1["verse_text"].startswith("Now there was")
        )

    def test_extracts_heading_before_first_verse(self):
        result = _parse_chapter_content(_CHAPTER_CONTENT)
        self.assertEqual(len(result["headings"]), 1)
        heading = result["headings"][0]
        self.assertEqual(heading["before_verse"], 1)
        self.assertEqual(
            heading["text"], "Jesus Teaches Nicodemus"
        )

    def test_empty_content_returns_no_verses(self):
        result = _parse_chapter_content([])
        self.assertEqual(result["verses"], [])
        self.assertEqual(result["headings"], [])

    def test_text_before_first_verse_is_ignored(self):
        content = [
            {"type": "text", "text": "Preamble text"},
            {"type": "verse", "verseId": "GEN.1.1"},
            {"type": "text", "text": "In the beginning..."},
        ]
        result = _parse_chapter_content(content)
        self.assertEqual(len(result["verses"]), 1)
        self.assertEqual(
            result["verses"][0]["verse_text"],
            "In the beginning...",
        )


# ============================================================
# Client — construction and HTTP calls
# ============================================================

class ApiBibleClientSessionTests(TestCase):
    def test_api_key_header_is_set(self):
        client = ApiBibleClient(api_key="test-key")
        self.assertEqual(
            client.session.headers["api-key"], "test-key"
        )

    @override_settings(API_BIBLE_KEY=None)
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            ApiBibleClient(api_key=None)
        self.assertIn(
            "API_BIBLE_KEY not configured", str(ctx.exception)
        )

    def test_get_chapter_calls_correct_url_and_params(self):
        client = ApiBibleClient(api_key="test-key")
        payload = {
            "data": {"content": _CHAPTER_CONTENT},
            "meta": _FUMS_META,
        }
        client.session.get = MagicMock(
            return_value=_json_response(payload)
        )

        result = client.get_chapter(BIBLE_ID, "JHN", 3)

        call_args, call_kwargs = client.session.get.call_args
        url = call_args[0]
        self.assertIn("api.scripture.api.bible", url)
        self.assertIn(f"/bibles/{BIBLE_ID}/chapters/JHN.3", url)
        params = call_kwargs.get("params", {})
        self.assertEqual(params["content-type"], "json")
        self.assertEqual(params["include-verse-numbers"], "true")
        self.assertEqual(params["include-verse-spans"], "true")
        self.assertEqual(params["include-titles"], "true")

        self.assertEqual(len(result["verses"]), 4)
        self.assertEqual(result["fums"]["fumsId"], "abc123")

    def test_get_chapter_empty_data_raises_not_found(self):
        client = ApiBibleClient(api_key="test-key")
        client.session.get = MagicMock(
            return_value=_json_response({"data": {}})
        )
        with self.assertRaises(PassageNotFoundError):
            client.get_chapter(BIBLE_ID, "JHN", 99)

    def test_get_chapter_audio_url(self):
        client = ApiBibleClient(api_key="test-key")
        payload = {
            "data": {
                "id": "JHN.3",
                "resourceUrl": (
                    "https://cdn.example.com/JHN.3.mp3"
                    "?X-Amz-Signature=abc"
                ),
                "timecodes": [
                    {"verseId": "JHN.3.1", "timestamp": 0.0},
                    {"verseId": "JHN.3.2", "timestamp": 12.5},
                ],
            }
        }
        client.session.get = MagicMock(
            return_value=_json_response(payload)
        )

        url = client.get_chapter_audio_url(
            AUDIO_BIBLE_ID, "JHN", 3
        )
        self.assertTrue(url.startswith("https://cdn.example.com"))
        call_args, _ = client.session.get.call_args
        self.assertIn(
            f"/audio-bibles/{AUDIO_BIBLE_ID}/chapters/JHN.3",
            call_args[0],
        )

    def test_get_chapter_audio_returns_timecodes(self):
        client = ApiBibleClient(api_key="test-key")
        payload = {
            "data": {
                "resourceUrl": "https://cdn.example.com/a.mp3",
                "timecodes": [
                    {"verseId": "JHN.3.1", "timestamp": 0.0},
                ],
            }
        }
        client.session.get = MagicMock(
            return_value=_json_response(payload)
        )
        audio = client.get_chapter_audio(
            AUDIO_BIBLE_ID, "JHN", 3
        )
        self.assertEqual(
            audio["audio_url"], "https://cdn.example.com/a.mp3"
        )
        self.assertEqual(len(audio["timecodes"]), 1)

    def test_get_chapter_audio_missing_url_raises(self):
        client = ApiBibleClient(api_key="test-key")
        client.session.get = MagicMock(
            return_value=_json_response({"data": {}})
        )
        with self.assertRaises(PassageNotFoundError):
            client.get_chapter_audio(AUDIO_BIBLE_ID, "JHN", 3)

    def test_search_uses_offset_params(self):
        client = ApiBibleClient(api_key="test-key")
        client.session.get = MagicMock(
            return_value=_json_response({
                "data": {
                    "query": "love",
                    "total": 1,
                    "verses": [
                        {
                            "id": "JHN.3.16",
                            "orgId": "JHN.3.16",
                            "bookId": "JHN",
                            "chapterId": "3",
                            "reference": "John 3:16",
                            "text": "For God so loved...",
                        }
                    ],
                }
            })
        )
        result = client.search(
            BIBLE_ID, "love", offset=15, limit=15
        )
        _, call_kwargs = client.session.get.call_args
        params = call_kwargs.get("params", {})
        self.assertEqual(params["query"], "love")
        self.assertEqual(params["offset"], "15")
        self.assertEqual(params["limit"], "15")
        self.assertEqual(result["data"]["total"], 1)


# ============================================================
# Error mapping via provider_error_fields
# ============================================================

class ApiBibleErrorMappingTests(TestCase):
    def _failing_client(self, status_code):
        client = ApiBibleClient(api_key="test-key")
        resp = MagicMock()
        resp.raise_for_status.side_effect = (
            _http_error(status_code)
        )
        client.session.get = MagicMock(return_value=resp)
        return client

    def test_404_maps_to_not_found(self):
        client = self._failing_client(404)
        with self.assertRaises(requests.HTTPError) as ctx:
            client.get_chapter(BIBLE_ID, "JHN", 3)
        fields = provider_error_fields(ctx.exception)
        self.assertEqual(fields["error_code"], "not_found")

    def test_429_maps_to_rate_limited(self):
        client = self._failing_client(429)
        with self.assertRaises(requests.HTTPError) as ctx:
            client.get_chapter(BIBLE_ID, "JHN", 3)
        fields = provider_error_fields(ctx.exception)
        self.assertEqual(fields["error_code"], "rate_limited")

    def test_401_and_403_map_to_provider_error(self):
        for code in (401, 403):
            client = self._failing_client(code)
            with self.assertRaises(requests.HTTPError) as ctx:
                client.get_chapter(BIBLE_ID, "JHN", 3)
            fields = provider_error_fields(ctx.exception)
            self.assertEqual(
                fields["error_code"], "provider_error"
            )
            # Exception text (which can contain URLs with
            # credentials) must never surface.
            self.assertNotIn("boom", fields["error"].lower())


# ============================================================
# Serializer routing
# ============================================================

class ApiBibleSerializerRoutingTests(TestCase):
    def _data(self, fileset_id, response_format="text"):
        return {
            "book": "JHN",
            "book_name": "John",
            "chapter": 3,
            "fileset_id": fileset_id,
            "response_format": response_format,
        }

    @patch("bible.serializers.get_default_apibible_client")
    def test_text_response_includes_verses_headings_fums(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_chapter.return_value = {
            "verses": [
                {"verse_start": 16, "verse_text": "For God..."}
            ],
            "headings": [
                {"before_verse": 16, "text": "God's Love"}
            ],
            "fums": _FUMS_META,
        }
        mock_get.return_value = mock_client

        data = self._data("ENGNIV_API")
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(result["format"], "text")
        self.assertEqual(
            result["verses"][0]["text"], "For God..."
        )
        self.assertEqual(
            result["headings"][0]["text"], "God's Love"
        )
        # FUMS meta passes through for frontend reporting.
        self.assertEqual(result["meta"]["fumsId"], "abc123")
        mock_client.get_chapter.assert_called_once_with(
            BIBLE_ID, "JHN", 3
        )

    @patch("bible.serializers.get_default_apibible_client")
    def test_audio_fileset_returns_audio_url(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_chapter_audio_url.return_value = (
            "https://cdn.example.com/JHN.3.mp3"
        )
        mock_get.return_value = mock_client

        data = self._data("ENGNIVC1DA", "audio")
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(result["format"], "audio")
        self.assertEqual(
            result["audio_url"],
            "https://cdn.example.com/JHN.3.mp3",
        )
        mock_client.get_chapter_audio_url.assert_called_once_with(
            AUDIO_BIBLE_ID, "JHN", 3
        )

    @patch("bible.serializers.get_default_apibible_client")
    def test_provider_failure_uses_error_contract(
        self, mock_get
    ):
        mock_get.side_effect = _http_error(429)
        data = self._data("ENGNIV_API")
        result = BiblePassageSerializer(data).to_representation(
            data
        )
        self.assertEqual(
            result["error_code"], "rate_limited"
        )
        self.assertNotIn("verses", result)


# ============================================================
# Translations endpoint includes NIV (no API key needed)
# ============================================================

class ApiBibleTranslationsEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="nivtest",
            email="niv@test.local",
            password="x",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch(
        "bible.services.translation_service."
        "get_default_dbt_client"
    )
    def test_translations_endpoint_includes_niv(
        self, mock_dbt
    ):
        mock_dbt.return_value.get_bibles.return_value = {
            "data": []
        }
        from django.urls import reverse
        url = reverse("translation-list")
        resp = self.client.get(
            url, {"language_iso": "eng"}
        )
        self.assertEqual(resp.status_code, 200)
        abbrs = [t["abbr"] for t in resp.json()["results"]]
        self.assertIn("NIV", abbrs)
        niv_entry = next(
            t for t in resp.json()["results"]
            if t["abbr"] == "NIV"
        )
        fileset_ids = [f["id"] for f in niv_entry["filesets"]]
        self.assertIn("ENGNIV_API", fileset_ids)
        self.assertIn("ENGNIVC1DA", fileset_ids)
