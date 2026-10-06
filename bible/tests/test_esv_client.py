"""Tests for the ESV API provider (Commits 1 & 2)."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from bible.services.esv.client import (
    ESVClient,
    ESV_AUDIO_BASE_URL,
    _parse_passage,
)
from bible.services.esv.registry import (
    get_esv_translation_listing,
    is_esv_fileset,
)
from bible.serializers import BiblePassageSerializer

User = get_user_model()

_SIMPLE_PASSAGE = (
    '<p><b class="chapter-num">1:1&nbsp;</b>In the beginning '
    'God created the heavens and the earth. '
    '<b class="verse-num">2&nbsp;</b>The earth was without form '
    'and void, and darkness was over the face of the deep.</p>'
)

_HEADED_PASSAGE = (
    '<h3>The Sermon on the Mount</h3>\n'
    '<p><b class="chapter-num">5:1&nbsp;</b>Seeing the crowds, '
    'he went up on the mountain.</p>\n'
    '<h3>The Beatitudes</h3>\n'
    '<p><b class="verse-num">3&nbsp;</b>Blessed are the poor in '
    'spirit. <b class="verse-num">4&nbsp;</b>Blessed are those '
    'who mourn.</p>'
)


# ============================================================
# Commit 1 — registry helpers
# ============================================================

class ESVRegistryTests(TestCase):
    def test_is_esv_fileset_true(self):
        self.assertTrue(is_esv_fileset("ENGESV_API"))

    def test_is_esv_fileset_case_insensitive(self):
        self.assertTrue(is_esv_fileset("engesv_api"))

    def test_is_esv_fileset_false_for_unknown(self):
        self.assertFalse(is_esv_fileset("ENGESV"))
        self.assertFalse(is_esv_fileset("LVSGLU8"))

    def test_translation_listing_shape(self):
        listing = get_esv_translation_listing()
        self.assertEqual(len(listing), 1)
        entry = listing[0]
        self.assertEqual(entry["abbr"], "ESV")
        self.assertEqual(entry["iso"], "eng")
        filesets = entry["filesets"]
        self.assertEqual(len(filesets), 2)
        types = {f["type"] for f in filesets}
        self.assertIn("text_plain", types)
        self.assertIn("audio", types)
        for f in filesets:
            self.assertEqual(f["id"], "ENGESV_API")
            self.assertEqual(f["size"], "C")


# ============================================================
# Commit 1 — parser (verse extraction)
# ============================================================

class ParsePassageVersesTests(TestCase):
    def test_parse_extracts_two_verses(self):
        result = _parse_passage(_SIMPLE_PASSAGE)
        verses = result["verses"]
        self.assertEqual(len(verses), 2)
        self.assertEqual(verses[0]["verse_start"], 1)
        self.assertIn("In the beginning", verses[0]["verse_text"])
        self.assertEqual(verses[1]["verse_start"], 2)
        self.assertIn("without form", verses[1]["verse_text"])

    def test_parse_returns_empty_headings_when_none(self):
        result = _parse_passage(_SIMPLE_PASSAGE)
        self.assertEqual(result["headings"], [])


# ============================================================
# Commit 1 — client session + URL
# ============================================================

class ESVClientSessionTests(TestCase):
    @override_settings(ESV_KEY="test-key")
    def test_get_chapter_verses_uses_session(self):
        client = ESVClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "passages": [_SIMPLE_PASSAGE]
        }
        mock_resp.raise_for_status = MagicMock()
        client.session.get = MagicMock(return_value=mock_resp)

        verses = client.get_chapter_verses("GEN", 1)

        client.session.get.assert_called_once()
        call_args, call_kwargs = client.session.get.call_args
        self.assertIn(
            "api.esv.org", call_args[0]
        )
        params = call_kwargs.get("params", {})
        self.assertIn("Genesis 1", params.get("q", ""))
        self.assertEqual(len(verses), 2)

    @override_settings(ESV_KEY="test-key")
    def test_authorization_header_is_set(self):
        client = ESVClient(api_key="test-key")
        self.assertIn(
            "Authorization", client.session.headers
        )
        self.assertEqual(
            client.session.headers["Authorization"],
            "Token test-key",
        )

    @override_settings(ESV_KEY=None)
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            ESVClient(api_key=None)


# ============================================================
# Commit 1 — serializer routing
# ============================================================

class ESVSerializerRoutingTests(TestCase):
    @override_settings(ESV_KEY="test-key")
    @patch(
        "bible.serializers.get_default_esv_client"
    )
    def test_serializer_routes_esv_fileset(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            "verses": [
                {"verse_start": 1, "verse_text": "ESV text."}
            ],
            "headings": [],
        }
        mock_get.return_value = mock_client

        data = {
            "book": "GEN",
            "book_name": "Genesis",
            "chapter": 1,
            "fileset_id": "ENGESV_API",
            "response_format": "text",
        }
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(result["format"], "text")
        self.assertEqual(len(result["verses"]), 1)
        self.assertEqual(
            result["verses"][0]["text"], "ESV text."
        )
        mock_client.get_chapter_with_headings.assert_called_once_with(
            "GEN", 1
        )

    @override_settings(ESV_KEY="test-key")
    @patch(
        "bible.serializers.get_default_esv_client"
    )
    def test_serializer_returns_esv_audio_url(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_chapter_audio_url.return_value = (
            "https://cdn.esv.org/audio/genesis_1.mp3"
        )
        mock_get.return_value = mock_client

        data = {
            "book": "GEN",
            "book_name": "Genesis",
            "chapter": 1,
            "fileset_id": "ENGESV_API",
            "response_format": "audio",
        }
        result = BiblePassageSerializer(data).to_representation(
            data
        )
        self.assertEqual(result["format"], "audio")
        self.assertEqual(
            result["audio_url"],
            "https://cdn.esv.org/audio/genesis_1.mp3",
        )
        mock_client.get_chapter_audio_url.assert_called_once_with(
            "GEN", 1
        )


# ============================================================
# get_chapter_audio_url — redirect handling
# ============================================================

class ESVClientAudioTests(TestCase):
    @override_settings(ESV_KEY="test-key")
    def test_returns_location_from_redirect(self):
        client = ESVClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {
            "Location": "https://cdn.esv.org/audio/jhn_3.mp3"
        }
        client.session.get = MagicMock(
            return_value=mock_resp
        )

        url = client.get_chapter_audio_url("JHN", 3)

        self.assertEqual(
            url, "https://cdn.esv.org/audio/jhn_3.mp3"
        )
        call_args, call_kwargs = client.session.get.call_args
        self.assertIn(ESV_AUDIO_BASE_URL, call_args)
        params = call_kwargs.get("params", {})
        self.assertIn("John 3", params.get("q", ""))
        self.assertFalse(
            call_kwargs.get("allow_redirects", True)
        )

    @override_settings(ESV_KEY="test-key")
    def test_raises_if_no_location_header(self):
        client = ESVClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {}
        client.session.get = MagicMock(
            return_value=mock_resp
        )

        with self.assertRaises(ValueError):
            client.get_chapter_audio_url("GEN", 1)

    @override_settings(ESV_KEY="test-key")
    def test_raises_on_non_redirect_status(self):
        client = ESVClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        client.session.get = MagicMock(
            return_value=mock_resp
        )

        with self.assertRaises(ValueError):
            client.get_chapter_audio_url("GEN", 1)


# ============================================================
# Commit 1 — translations endpoint includes ESV
# ============================================================

class ESVTranslationsEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="esvtest",
            email="esv@test.local",
            password="x",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch(
        "bible.services.translation_service."
        "get_default_dbt_client"
    )
    def test_translations_endpoint_includes_esv(
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
        self.assertIn("ESV", abbrs)
        esv_entry = next(
            t for t in resp.json()["results"]
            if t["abbr"] == "ESV"
        )
        fileset_ids = [
            f["id"] for f in esv_entry["filesets"]
        ]
        self.assertIn("ENGESV_API", fileset_ids)


# ============================================================
# Commit 2 — structured headings parse
# ============================================================

class ParsePassageHeadingsTests(TestCase):
    def test_parse_extracts_headings_at_chapter_start(self):
        passage = (
            '<h3>The Sermon on the Mount</h3>\n'
            '<p><b class="chapter-num">5:1&nbsp;</b>Seeing the '
            'crowds, he went up.</p>'
        )
        result = _parse_passage(passage)
        self.assertEqual(len(result["headings"]), 1)
        self.assertEqual(
            result["headings"][0]["before_verse"], 1
        )
        self.assertEqual(
            result["headings"][0]["text"],
            "The Sermon on the Mount",
        )

    def test_parse_extracts_mid_chapter_heading(self):
        result = _parse_passage(_HEADED_PASSAGE)
        headings = result["headings"]
        texts = [h["text"] for h in headings]
        self.assertIn("The Sermon on the Mount", texts)
        self.assertIn("The Beatitudes", texts)
        beatitudes = next(
            h for h in headings
            if h["text"] == "The Beatitudes"
        )
        self.assertEqual(beatitudes["before_verse"], 3)

    def test_parse_returns_empty_headings_when_none(self):
        result = _parse_passage(_SIMPLE_PASSAGE)
        self.assertEqual(result["headings"], [])

    def test_parse_verses_present_with_headings(self):
        result = _parse_passage(_HEADED_PASSAGE)
        verse_nums = [
            v["verse_start"] for v in result["verses"]
        ]
        self.assertIn(1, verse_nums)
        self.assertIn(3, verse_nums)
        self.assertIn(4, verse_nums)


# ============================================================
# Commit 2 — serializer returns headings for ESV
# ============================================================

class ESVSerializerHeadingsTests(TestCase):
    @override_settings(ESV_KEY="test-key")
    @patch(
        "bible.serializers.get_default_esv_client"
    )
    def test_serializer_returns_headings_for_esv(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            "verses": [
                {"verse_start": 3, "verse_text": "Blessed..."}
            ],
            "headings": [
                {
                    "before_verse": 3,
                    "text": "The Beatitudes",
                }
            ],
        }
        mock_get.return_value = mock_client

        data = {
            "book": "MAT",
            "book_name": "Matthew",
            "chapter": 5,
            "fileset_id": "ENGESV_API",
            "response_format": "text",
        }
        result = BiblePassageSerializer(data).to_representation(
            data
        )
        self.assertIn("headings", result)
        self.assertEqual(len(result["headings"]), 1)
        self.assertEqual(
            result["headings"][0]["text"], "The Beatitudes"
        )
        self.assertEqual(
            result["headings"][0]["before_verse"], 3
        )


class ESVClientSearchTests(TestCase):
    """Tests for the ESV client search method."""

    @override_settings(ESV_KEY="test-key")
    @patch("bible.services.esv.client.requests.Session.get")
    def test_search_calls_esv_api_correctly(self, mock_get):
        """Search method should call ESV API with correct parameters."""
        # Mock the API response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "page": 1,
            "total_results": 2,
            "total_pages": 1,
            "results": [
                {
                    "reference": "John 3:16",
                    "content": "For God so loved the world"
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        client = ESVClient(api_key="test-key")
        result = client.search("love", page=1, page_size=50)

        # Verify the API was called correctly
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertIn("q=love", call_args[1]['params'])
        self.assertIn("page=1", call_args[1]['params'])
        self.assertIn("page-size=50", call_args[1]['params'])

        # Verify the result
        self.assertEqual(result["total_results"], 2)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["reference"], "John 3:16")

    @override_settings(ESV_KEY="test-key")
    @patch("bible.services.esv.client.requests.Session.get")
    def test_search_limits_page_size_to_100(self, mock_get):
        """Search method should limit page_size to ESV API maximum of 100."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        client = ESVClient(api_key="test-key")
        client.search("test", page=1, page_size=200)  # Should be limited
        # to 100

        call_args = mock_get.call_args
        self.assertIn("page-size=100", call_args[1]['params'])

    @override_settings(ESV_KEY="test-key")
    @patch("bible.services.esv.client.requests.Session.get")
    def test_search_handles_api_errors(self, mock_get):
        """Search method should raise HTTPError for API errors."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("API Error")
        mock_get.return_value = mock_response

        client = ESVClient(api_key="test-key")

        with self.assertRaises(Exception) as context:
            client.search("test")

        self.assertIn("API Error", str(context.exception))

    def test_search_requires_api_key(self):
        """Search method should raise ValueError if no API key is set."""
        with self.assertRaises(ValueError) as context:
            ESVClient(api_key=None)

        self.assertIn("ESV_KEY not configured", str(context.exception))


# ============================================================
# Issue #66 — Poetry, quoted scripture, and headings must
# not be confused
# ============================================================

class ParsePassagePoetryAndQuotesTests(TestCase):
    """Test cases for GitHub issue #66.

    Daniel 7:13-15 poetry and Luke 4:8-12 quoted scripture
    must stay inside their verses, while the real section
    heading between them must be extracted as a heading —
    not glued onto the preceding verse text.
    """

    # Mirrors the actual api.esv.org passage/html markup.
    _DANIEL_7 = (
        '<h3>The Son of Man Is Given Dominion</h3>\n'
        '<p><b class="verse-num">13&nbsp;</b>“I saw in the '
        'night visions,</p>\n'
        '<p class="block-indent">'
        '<span class="begin-line-group"></span>\n'
        '<span class="line">&nbsp;&nbsp;and behold, with the '
        'clouds of heaven</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'there came one like a son of man,</span><br />'
        '<span class="line">&nbsp;&nbsp;and he came to the '
        'Ancient of Days</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'and was presented before him.</span><br />'
        '<span class="line"><b class="verse-num inline">'
        '14&nbsp;</b>&nbsp;&nbsp;And to him was given '
        'dominion</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'and glory and a kingdom,</span><br />'
        '<span class="line">&nbsp;&nbsp;that all peoples, '
        'nations, and languages</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'should serve him;</span><br />'
        '<span class="line">&nbsp;&nbsp;his dominion is an '
        'everlasting dominion,</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'which shall not pass away,</span><br />'
        '<span class="line">&nbsp;&nbsp;and his kingdom one'
        '</span><br />'
        '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
        'that shall not be destroyed.</span><br />'
        '<span class="end-line-group"></span>\n'
        '</p>'
        '<h3>Daniel’s Vision Interpreted</h3>\n'
        '<p><b class="verse-num">15&nbsp;</b>“As for me, '
        'Daniel, my spirit within me was anxious, and the '
        'visions of my head alarmed me.</p>'
    )

    def test_daniel_7_verse_13_continuation_not_heading(self):
        """Daniel 7:13-14 poetry stays in the verses."""
        result = _parse_passage(self._DANIEL_7)

        verses = result["verses"]
        verse_nums = [v["verse_start"] for v in verses]
        self.assertEqual(verse_nums, [13, 14, 15])

        verse_13_text = verses[0]["verse_text"]
        self.assertIn("I saw in the night visions", verse_13_text)
        self.assertIn("and behold", verse_13_text)
        self.assertIn("clouds of heaven", verse_13_text)
        self.assertIn("presented before him", verse_13_text)

        verse_14_text = verses[1]["verse_text"]
        self.assertIn("given dominion", verse_14_text)
        self.assertIn("shall not be destroyed", verse_14_text)

    def test_daniel_7_mid_poetry_heading_not_in_verse(self):
        """'Daniel's Vision Interpreted' is a heading before
        verse 15 — it must NOT be glued onto verse 14."""
        result = _parse_passage(self._DANIEL_7)

        headings = result["headings"]
        texts = {h["before_verse"]: h["text"] for h in headings}
        self.assertEqual(
            texts.get(13), "The Son of Man Is Given Dominion"
        )
        self.assertEqual(
            texts.get(15), "Daniel’s Vision Interpreted"
        )

        verse_14 = next(
            v for v in result["verses"] if v["verse_start"] == 14
        )
        self.assertNotIn("Interpreted", verse_14["verse_text"])

    def test_luke_4_quoted_scripture_not_headings(self):
        """Luke 4:8-12 quoted scripture should not be parsed
        as headings."""
        # Mirrors the actual api.esv.org passage/html markup.
        passage = (
            '<p class="virtual">'
            '<b class="verse-num">8&nbsp;</b>And Jesus answered '
            'him, <span class="woc">“It is written,</span></p>\n'
            '<p class="block-indent">'
            '<span class="begin-line-group"></span>\n'
            '<span class="line">&nbsp;&nbsp;<span class="woc">'
            '“‘You shall worship the Lord your God,</span>'
            '</span><br />'
            '<span class="indent line"><span class="woc">'
            '&nbsp;&nbsp;&nbsp;&nbsp;and him only shall you '
            'serve.’”</span></span><br />'
            '<span class="end-line-group"></span>\n'
            '</p><p class="same-paragraph">'
            '<b class="verse-num">9&nbsp;</b>And he took him to '
            'Jerusalem and said to him, “If you are the Son of '
            'God, throw yourself down from here, '
            '<b class="verse-num">10&nbsp;</b>for it is written,'
            '</p>\n'
            '<p class="block-indent">'
            '<span class="line">&nbsp;&nbsp;“‘He will command '
            'his angels concerning you,</span><br />'
            '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
            'to guard you,’</span><br />'
            '<span class="end-line-group"></span>\n'
            '</p><p class="same-paragraph">'
            '<b class="verse-num">11&nbsp;</b>and</p>\n'
            '<p class="block-indent">'
            '<span class="line">&nbsp;&nbsp;“‘On their hands '
            'they will bear you up,</span><br />'
            '<span class="indent line">&nbsp;&nbsp;&nbsp;&nbsp;'
            'lest you strike your foot against a stone.’”'
            '</span><br />'
            '<span class="end-line-group"></span>\n'
            '</p><p class="same-paragraph">'
            '<b class="verse-num">12&nbsp;</b>And Jesus '
            'answered him, <span class="woc">“It is said, '
            '‘You shall not put the Lord your God to the '
            'test.’”</span></p>'
        )
        result = _parse_passage(passage)

        # Should have NO headings (all text is part of verses)
        self.assertEqual(result["headings"], [])

        # Should have 5 verses (8, 9, 10, 11, 12)
        verses = result["verses"]
        verse_nums = [v["verse_start"] for v in verses]
        self.assertEqual(verse_nums, [8, 9, 10, 11, 12])

        # Verse 8 should include the quoted scripture
        verse_8 = next(v for v in verses if v["verse_start"] == 8)
        self.assertIn("It is written", verse_8["verse_text"])
        self.assertIn(
            "You shall worship the Lord your God",
            verse_8["verse_text"]
        )
