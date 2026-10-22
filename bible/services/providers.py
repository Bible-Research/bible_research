"""Provider facade: the single ``fileset_id`` routing point.

Every bible capability — chapter/passage text, chapter audio,
search, timestamps, copyright, translation listing — used to
re-implement the ``is_esv → is_apibible → is_sword → else DBT``
chain at each call site (passage serializer, views, note
enrichment, fileset grouping). ``provider_for(fileset_id)``
returns the provider object selected by an id; each provider
exposes the same capability surface so callers never branch on
provider type.

Normalized exception taxonomy (callers map these through
``bible.utils.provider_errors``):

- ``PassageNotFoundError`` — the fileset does not cover the
  requested book/chapter/verse selection (upstream 404s also
  classify as coverage misses via ``upstream_status_code``).
- ``TimestampsNotGeneratedError`` — a ``PassageNotFoundError``
  subclass for "audio exists but its timestamp sidecar was not
  generated yet"; lets the timestamps endpoint keep its own
  message while remaining a coverage miss elsewhere.
- Every other exception propagates unchanged and is classified
  by ``provider_error_fields`` (``rate_limited`` /
  ``not_found`` / ``provider_error``). Exception text is never
  embedded in a response body — it can contain request URLs
  carrying credentials.

Normalized chapter payload (``get_chapter``)::

    {
        "verses":   [{"verse_start": int, "verse_text": str}],
        "headings": [{"before_verse": int, "text": str}] | None,
        "meta":     { ... } | {},
    }

``headings`` is ``None`` for providers with no heading metadata
(SWORD); ``meta`` carries provider-mandated relay data — e.g.
API.Bible's FUMS block, which the frontend must report per
license terms.
"""
import logging

from django.conf import settings
from google.api_core import exceptions as gcs_exceptions

from bible.services.apibible.client import (
    get_default_apibible_client,
)
from bible.services.apibible.registry import (
    get_apibible_meta,
    get_apibible_translation_listing,
    is_apibible_fileset,
)
from bible.services.dbt.client import get_default_dbt_client
from bible.services.esv.client import get_default_esv_client
from bible.services.esv.registry import (
    canonical_esv_fileset_id,
    get_esv_translation_listing,
    is_esv_fileset,
    ESV_TRANSLATIONS,
)
from bible.services.google_tts.registry import get_tts_config
from bible.services.search_grouping import (
    MAX_RESULTS,
    fetch_apibible_matches,
    fetch_dbt_matches,
    fetch_esv_matches,
    group_verses_by_book,
    normalize_apibible_verse,
    normalize_dbt_verse,
    parse_esv_results,
    scan_sword_matches,
)
from bible.services.storage import gcs
from bible.services.sword.client import get_default_sword_client
from bible.services.sword.registry import (
    audio_mode_for_fileset,
    canonical_sword_fileset_id,
    get_sword_meta,
    is_sword_fileset,
)
from bible.utils.provider_errors import (
    PassageNotFoundError,
    TimestampsNotGeneratedError,
    provider_error_fields,
    upstream_status_code,
)

logger = logging.getLogger(__name__)


def provider_for(fileset_id):
    """Return the provider object selected by ``fileset_id``.

    Dispatch order is load-bearing and preserved from the old
    inline chains: ESV → API.Bible → SWORD → everything else DBT.
    """
    if is_esv_fileset(fileset_id):
        return ESVProvider(fileset_id)
    if is_apibible_fileset(fileset_id):
        return ApiBibleProvider(fileset_id)
    if is_sword_fileset(fileset_id):
        return SwordProvider(fileset_id)
    return DbtProvider(fileset_id)


class Provider:
    """Uniform capability surface over the Bible providers."""

    # Grouping metadata used by fileset_groups: ``audio_codec``
    # is the codec slot a non-DBT audio fileset occupies in
    # ``by_testament`` maps (None = the id grammar decides);
    # ``audio_kind`` is the option ``kind`` such a fileset
    # produces.
    audio_codec = None
    audio_kind = 'audio'

    def __init__(self, fileset_id=None):
        self.fileset_id = fileset_id

    def get_chapter(self, book_id, chapter,
                    verse_start=None, verse_end=None):
        """Return the normalized chapter payload (see module
        docstring). Raises ``PassageNotFoundError`` when the
        fileset does not cover the chapter.

        ``verse_start``/``verse_end`` are an optional narrowing
        hint — providers without an upstream range parameter
        ignore them and callers filter client-side."""
        raise NotImplementedError

    def get_audio(self, book_id, chapter):
        """Return an audio payload ``{audio_url, ...}``."""
        raise NotImplementedError

    def get_passage(
        self, book_id, chapter, response_format,
        verse_start=None, verse_end=None,
    ):
        """Return the passage payload used by the passage
        endpoint: ``format`` plus either ``verses``/``headings``/
        ``meta`` or the audio fields. Default implementation
        composes ``get_chapter``/``get_audio``; DBT overrides it
        because one upstream call serves both formats."""
        if response_format == 'audio':
            payload = dict(self.get_audio(book_id, chapter))
            payload['format'] = 'audio'
            return payload
        parsed = self.get_chapter(
            book_id, chapter,
            verse_start=verse_start, verse_end=verse_end,
        )
        payload = {'format': 'text', 'verses': parsed['verses']}
        if parsed.get('headings') is not None:
            payload['headings'] = parsed['headings']
        if parsed.get('meta'):
            payload['meta'] = parsed['meta']
        return payload

    def get_timestamps(self, book_id, chapter):
        """Return ``[{verse_start, timestamp}, ...]``.

        Raises ``TimestampsNotGeneratedError`` when the sidecar
        artifact does not exist yet."""
        raise NotImplementedError

    def get_copyright(self):
        """Return copyright entries shaped like the DBT
        ``/copyright`` response rows: ``{id, type, size,
        copyright, copyright_date, copyright_description}``."""
        raise NotImplementedError

    def search(self, query, limit, page, sort_by=None, books=None):
        """Return ``{'verses': [...], 'meta': {...}}`` for one
        page of search results."""
        raise NotImplementedError

    def search_grouped(self, query, books=None, sort_by=None):
        """Return ``{'groups': [...], 'meta': {...}}`` — every
        match grouped per book (``group_by=book`` mode).

        ``sort_by`` is provider-specific (DBT only); providers
        without an upstream sort parameter ignore it."""
        raise NotImplementedError

    def translation_listing(self, language_iso=None):
        """This provider's contributions to the translations
        endpoint (registry entries or raw DBT translation
        rows)."""
        raise NotImplementedError


def _registry_copyright(fileset_id, license_text):
    """One flattened copyright row for a registry-served id —
    same shape the API.Bible branch returns from its live
    ``get_bible`` call."""
    return [{
        'id': fileset_id,
        'type': 'text_plain',
        'size': 'C',
        'copyright': license_text,
        'copyright_date': '',
        'copyright_description': license_text,
    }]


def _filter_by_iso(entries, language_iso):
    """Keep only entries whose ``iso`` matches (case-insensitive)."""
    if not language_iso:
        return entries
    needle = language_iso.lower()
    return [
        e for e in entries
        if (e.get('iso') or '').lower() == needle
    ]


def _apibible_timecode(item):
    """Map an API.Bible audio timecode to ``{verse_start,
    timestamp}``.

    Timecode entries carry a ``verseId`` like ``JHN.3.16`` (some
    payloads expose ``verse_start``/``verse``/``verse_id``
    directly); the trailing dotted segment is the verse number.
    API.Bible serializes numbers as strings, so both fields are
    coerced to real ``int``/``float`` values.
    """
    verse = (
        item.get('verse_start')
        or item.get('verse')
        or item.get('verse_id')
    )
    if verse is not None:
        try:
            verse = int(verse)
        except (TypeError, ValueError):
            verse = None
    if verse is None:
        verse_id = item.get('verseId') or ''
        try:
            # Strip the range tail first: both "JHN.3.16-17"
            # and "JHN.3.16-JHN.3.17" must resolve to 16.
            verse = int(
                str(verse_id).split('-', 1)[0].split('.')[-1]
            )
        except ValueError:
            verse = None
    timestamp = item.get('timestamp')
    if timestamp is not None:
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = None
    return {
        'verse_start': verse,
        'timestamp': timestamp,
    }


def _no_verses(book_id, chapter, fileset_id):
    return PassageNotFoundError(
        f"No verses found for {book_id} {chapter} "
        f"in fileset_id={fileset_id}"
    )


def _fallback(value, default):
    """``value`` unless it is ``None`` — distinguishes "absent"
    from a legitimate ``0`` where ``or`` would misfire."""
    return value if value is not None else default


class ESVProvider(Provider):
    """api.esv.org text/audio (``ENGESV_API``).

    Timestamps fall through to the DBT endpoint (upstream
    behaviour the old view inline branch also relied on)."""

    audio_codec = 'api'
    audio_kind = 'audio'

    def get_chapter(self, book_id, chapter,
                    verse_start=None, verse_end=None):
        client = get_default_esv_client()
        if verse_start is not None:
            parsed = client.get_chapter_with_headings(
                book_id, chapter,
                verse_start=verse_start, verse_end=verse_end,
            )
        else:
            parsed = client.get_chapter_with_headings(
                book_id, chapter
            )
        verses = parsed.get('verses') or []
        if not verses:
            raise _no_verses(book_id, chapter, self.fileset_id)
        return {
            'verses': [
                {
                    'verse_start': v['verse_start'],
                    'verse_text': v['verse_text'],
                }
                for v in verses
            ],
            'headings': parsed.get('headings') or [],
            'meta': {},
        }

    def get_audio(self, book_id, chapter):
        return {
            'audio_url': (
                get_default_esv_client()
                .get_chapter_audio_url(book_id, chapter)
            ),
        }

    def get_timestamps(self, book_id, chapter):
        # ESV audio filesets are served by DBT timestamps
        # upstream — same delegation the old inline branch used.
        return DbtProvider(self.fileset_id).get_timestamps(
            book_id, chapter
        )

    def get_copyright(self):
        canon = canonical_esv_fileset_id(self.fileset_id)
        return _registry_copyright(
            self.fileset_id,
            ESV_TRANSLATIONS[canon]['license'],
        )

    def search(self, query, limit, page, sort_by=None, books=None):
        result = get_default_esv_client().search(query, page, limit)
        verses = parse_esv_results(result.get('results', []))
        pagination = {
            'total': result.get('total_results', 0),
            'count': len(verses),
            'per_page': limit,
            'current_page': result.get('page', page),
            'total_pages': result.get('total_pages', 1),
        }
        return {
            'verses': verses,
            'meta': {'pagination': pagination},
        }

    def search_grouped(self, query, books=None, sort_by=None):
        verses, total, truncated = fetch_esv_matches(
            get_default_esv_client(), query
        )
        return {
            'groups': group_verses_by_book(verses),
            'meta': {'total': total, 'truncated': truncated},
        }

    def translation_listing(self, language_iso=None):
        return _filter_by_iso(
            get_esv_translation_listing(), language_iso
        )


class ApiBibleProvider(Provider):
    """api.scripture.api.bible text/audio (``ENGNIV_API``,
    ``ENGNIVC1DA``; abbrs like ``NIV`` canonicalize)."""

    audio_codec = None
    audio_kind = 'audio'

    def __init__(self, fileset_id=None):
        super().__init__(fileset_id)
        self._meta = (
            get_apibible_meta(fileset_id) if fileset_id else None
        )

    def get_chapter(self, book_id, chapter,
                    verse_start=None, verse_end=None):
        # API.Bible has no verse-range query param — the
        # narrowing hint is ignored and selection is applied
        # client-side by ``get_passage``/``_select_verses``.
        parsed = get_default_apibible_client().get_chapter(
            self._meta['bible_id'], book_id, chapter
        )
        verses = parsed.get('verses') or []
        if not verses:
            raise _no_verses(book_id, chapter, self.fileset_id)
        return {
            'verses': [
                {
                    'verse_start': v['verse_start'],
                    'verse_text': v['verse_text'],
                }
                for v in verses
            ],
            'headings': parsed.get('headings') or [],
            # FUMS tracking token — relaying it to the frontend is
            # an API.Bible license requirement.
            'meta': parsed.get('fums') or {},
        }

    def get_audio(self, book_id, chapter):
        audio = get_default_apibible_client().get_chapter_audio(
            self._meta['audio_bible_id'], book_id, chapter
        )
        payload = {'audio_url': audio['audio_url']}
        fums = audio.get('fums')
        if fums:
            payload['meta'] = fums
        return payload

    def get_timestamps(self, book_id, chapter):
        audio = get_default_apibible_client().get_chapter_audio(
            self._meta['audio_bible_id'], book_id, chapter
        )
        timecodes = audio.get('timecodes') or []
        # Drop entries whose verse reference or timestamp failed
        # to parse — emitting ``null`` fields would push the
        # malformed upstream row onto clients.
        mapped = [
            tc for tc in (
                _apibible_timecode(tc) for tc in timecodes
            )
            if tc['verse_start'] is not None
            and tc['timestamp'] is not None
        ]
        if not mapped:
            raise TimestampsNotGeneratedError(
                f"No timecodes for {book_id} {chapter} "
                f"in fileset_id={self.fileset_id}"
            )
        return mapped

    def get_copyright(self):
        # Audio fileset ids (``ENGNIVC1DA``) carry their own
        # audio bible upstream — copyright for them must read
        # the audio bible's metadata rather than echoing the
        # text bible's statement.
        is_audio = (
            self.fileset_id is not None
            and self.fileset_id.strip().upper()
            == self._meta['audio_fileset_id'].upper()
        )
        bible = get_default_apibible_client().get_bible(
            self._meta['audio_bible_id'] if is_audio
            else self._meta['bible_id']
        )
        copyright_text = (
            bible.get('copyrightStatement')
            or bible.get('copyright')
            or ''
        )
        return [{
            'id': self.fileset_id,
            'type': 'audio' if is_audio else 'text_plain',
            'size': 'C',
            'copyright': copyright_text,
            'copyright_date': '',
            'copyright_description': copyright_text,
        }]

    def search(self, query, limit, page, sort_by=None, books=None):
        offset = (page - 1) * limit
        result = get_default_apibible_client().search(
            self._meta['bible_id'], query,
            offset=offset, limit=limit,
        )
        data = result.get('data') or {}
        verses = [
            normalize_apibible_verse(v)
            for v in data.get('verses') or []
        ]
        total = data.get('total') or 0
        total_pages = (
            (total + limit - 1) // limit if limit else 1
        )
        return {
            'verses': verses,
            'meta': {
                'pagination': {
                    'total': total,
                    'count': len(verses),
                    'per_page': limit,
                    'current_page': page,
                    'total_pages': total_pages,
                }
            },
        }

    def search_grouped(self, query, books=None, sort_by=None):
        verses, total, truncated = fetch_apibible_matches(
            get_default_apibible_client(),
            self._meta['bible_id'], query,
        )
        return {
            'groups': group_verses_by_book(verses),
            'meta': {'total': total, 'truncated': truncated},
        }

    def translation_listing(self, language_iso=None):
        return _filter_by_iso(
            get_apibible_translation_listing(), language_iso
        )


class SwordProvider(Provider):
    """Locally-bundled SWORD modules + generated GCS TTS audio
    (``LVSGLU8``, ``LVSGLU8C1DA``, ``LVSGLU8C2DA``; abbr
    ``GLU8``)."""

    audio_codec = 'generated'
    audio_kind = 'generated'

    @property
    def _canon(self):
        return canonical_sword_fileset_id(self.fileset_id)

    def get_chapter(self, book_id, chapter,
                    verse_start=None, verse_end=None):
        client = get_default_sword_client()
        try:
            if verse_start is not None:
                verses = client.get_chapter_verses(
                    self.fileset_id, book_id, chapter,
                    verse_start=verse_start, verse_end=verse_end,
                )
            else:
                verses = client.get_chapter_verses(
                    self.fileset_id, book_id, chapter
                )
        except ValueError as e:
            # ``is_sword_fileset`` guarantees the id is known, so
            # a ValueError here means the book/chapter is outside
            # the module's coverage. Normalize the client's
            # miss vocabulary (it raises ValueError /
            # PassageNotFoundError). FileNotFoundError — a
            # missing module zip — is an OSError and still falls
            # through to the generic provider_error mapping.
            raise PassageNotFoundError(str(e)) from e
        return {
            'verses': [
                {
                    'verse_start': v['verse_start'],
                    'verse_text': v['verse_text'],
                }
                for v in verses
            ],
            # SWORD modules carry no headings metadata.
            'headings': None,
            'meta': {},
        }

    def _audio_paths(self, book_id, chapter):
        """(canon, voice_name, audio_mode) for the GCS artifacts
        this fileset id reads from."""
        canon = self._canon
        voice_name = get_tts_config(canon)["voice_name"]
        # The audio fileset id selects the generation mode:
        # LVSGLU8C1DA -> verse-mode artifacts under audio/,
        # LVSGLU8C2DA -> chapter-mode under audio_chapter/.
        audio_mode = audio_mode_for_fileset(self.fileset_id)
        return canon, voice_name, audio_mode

    def get_audio(self, book_id, chapter):
        canon, voice_name, audio_mode = self._audio_paths(
            book_id, chapter
        )
        if not gcs.chapter_audio_exists(
            canon, book_id, chapter, voice_name,
            mode=audio_mode,
        ):
            raise PassageNotFoundError(
                f"No generated audio for {book_id} {chapter} "
                f"in fileset_id={self.fileset_id}",
                client_message=(
                    'Audio not yet generated for this chapter'
                ),
            )
        timestamps = gcs.read_timestamps_json(
            canon, book_id, chapter, voice_name,
            mode=audio_mode,
        )
        # ``file_size_bytes`` is embedded by the generator
        # (see gcs.upload_chapter_artifacts) so the request
        # path needs no separate blob.metadata HEAD. Older
        # artifacts without that key fall back to a live
        # lookup for one release of backwards compatibility.
        file_size_bytes = timestamps.get('file_size_bytes')
        if file_size_bytes is None:
            audio_path, _ = gcs.chapter_object_paths(
                canon, book_id, chapter, voice_name,
                mode=audio_mode,
            )
            blob = gcs.get_default_client().bucket(
                settings.AUDIO_BUCKET_NAME
            ).get_blob(audio_path)
            file_size_bytes = blob.size if blob else None
        return {
            'audio_url': gcs.signed_audio_url(
                canon,
                book_id,
                chapter,
                voice_name,
                settings.AUDIO_SIGNED_URL_TTL_SECONDS,
                mode=audio_mode,
            ),
            'duration_seconds': timestamps.get('duration_seconds'),
            'file_size_bytes': file_size_bytes,
        }

    def get_timestamps(self, book_id, chapter):
        canon, voice_name, audio_mode = self._audio_paths(
            book_id, chapter
        )
        try:
            payload = gcs.read_timestamps_json(
                canon, book_id, int(chapter), voice_name,
                mode=audio_mode,
            )
        except gcs_exceptions.NotFound as e:
            raise TimestampsNotGeneratedError(str(e)) from e
        return payload.get('data') or []

    def get_copyright(self):
        return _registry_copyright(
            self.fileset_id,
            get_sword_meta(self.fileset_id)['license'],
        )

    def search(self, query, limit, page, sort_by=None, books=None):
        # Bound the local scan to what this page needs — a search
        # that matches ~31k verses used to read every chapter in
        # the module to answer page 1 (S7/search #5). When the
        # cap stops the scan early, ``truncated`` marks ``total``
        # as a lower bound so callers keep paging.
        matches, truncated = scan_sword_matches(
            get_default_sword_client(),
            self.fileset_id, query, books,
            max_matches=page * limit,
        )
        total = len(matches)
        start = (page - 1) * limit
        page_items = matches[start:start + limit]
        total_pages = (
            (total + limit - 1) // limit if limit else 1
        )
        return {
            'verses': page_items,
            'meta': {
                'pagination': {
                    'total': total,
                    'count': len(page_items),
                    'per_page': limit,
                    'current_page': page,
                    'total_pages': total_pages,
                },
                'truncated': truncated,
            },
        }

    def search_grouped(self, query, books=None, sort_by=None):
        # Grouped mode returns every match — cap it at the same
        # MAX_RESULTS bound the other providers honor instead of
        # scanning a whole-Bible result set into memory.
        verses, truncated = scan_sword_matches(
            get_default_sword_client(),
            self.fileset_id, query, books,
            max_matches=MAX_RESULTS,
        )
        return {
            'groups': group_verses_by_book(verses),
            'meta': {
                'total': len(verses),
                'truncated': truncated,
            },
        }

    def translation_listing(self, language_iso=None):
        return _filter_by_iso(
            get_default_sword_client().get_translation_listing(),
            language_iso,
        )


class DbtProvider(Provider):
    """DBT API v4 (b4.dbt.io) — the fallback for every fileset id
    the registries do not claim."""

    audio_codec = None
    audio_kind = 'audio'

    def _chapter_rows(self, book_id, chapter, chapter_as_str):
        """Raw ``data`` rows for the chapter; raises
        ``PassageNotFoundError`` on an empty body."""
        data = get_default_dbt_client().get_verses(
            book_id,
            str(chapter) if chapter_as_str else chapter,
            bible_id=self.fileset_id,
        )
        rows = (data or {}).get('data') or []
        if not rows:
            raise _no_verses(book_id, chapter, self.fileset_id)
        return data, rows

    def get_chapter(self, book_id, chapter,
                    verse_start=None, verse_end=None):
        """Whole-chapter payload for note enrichment — fetched
        once per (fileset, book, chapter) upstream, so no
        ``verse_start``/``verse_end`` narrowing is sent."""
        data, rows = self._chapter_rows(
            book_id, chapter, chapter_as_str=False
        )
        verses = [
            {
                'verse_start': v.get('verse_start'),
                'verse_text': v.get('verse_text', ''),
            }
            for v in rows
            if 'verse_text' in v
        ]
        if not verses:
            raise _no_verses(book_id, chapter, self.fileset_id)
        return {
            'verses': verses,
            'headings': data.get('headings') or [],
            'meta': {},
        }

    def get_passage(
        self, book_id, chapter, response_format,
        verse_start=None, verse_end=None,
    ):
        """One ``get_verses`` call serves both formats: for audio
        filesets (``*DA`` ids) the first row carries ``path``;
        verse filters could exclude whole-chapter audio files
        and turn a valid request into a spurious 404, so rows
        are post-filtered client-side for text."""
        _data, rows = self._chapter_rows(
            book_id, chapter, chapter_as_str=True
        )
        if 'path' in rows[0]:
            audio_data = rows[0]
            if (
                response_format == 'audio'
                and not audio_data.get('path')
            ):
                raise _no_verses(
                    book_id, chapter, self.fileset_id
                )
            return {
                'format': 'audio',
                'audio_url': audio_data.get('path'),
                'duration_seconds': audio_data.get('duration'),
                'file_size_bytes': audio_data.get(
                    'filesize_in_bytes'
                ),
            }
        if response_format == 'audio':
            # The fileset has no audio rows for this passage
            # (e.g. an audio request against a text fileset).
            raise _no_verses(book_id, chapter, self.fileset_id)
        return {
            'format': 'text',
            'verses': [
                {
                    'verse_start': v['verse_start'],
                    'verse_text': v.get('verse_text', ''),
                }
                for v in rows
                if 'verse_text' in v
            ],
        }

    def get_audio(self, book_id, chapter):
        payload = self.get_passage(
            book_id, chapter, response_format='audio'
        )
        return {
            k: v for k, v in payload.items() if k != 'format'
        }

    def get_timestamps(self, book_id, chapter):
        result = get_default_dbt_client().get_timestamps(
            self.fileset_id, book_id, chapter
        )
        return [
            {
                'verse_start': item.get('verse_start'),
                'timestamp': item.get('timestamp'),
            }
            for item in (result.get('data') or [])
        ]

    def get_copyright(self):
        result = get_default_dbt_client().get_copyright(
            self.fileset_id
        )
        filesets = []
        for item in (result or []):
            # Defensive: a malformed DBT row (non-dict item, or a
            # non-dict ``copyright`` payload) must not turn into
            # an AttributeError → misleading 502.
            if not isinstance(item, dict):
                continue
            cr = item.get('copyright') or {}
            if not isinstance(cr, dict):
                cr = {}
            filesets.append({
                'id': item.get('id'),
                'type': item.get('type'),
                'size': item.get('size'),
                'copyright': cr.get('copyright', ''),
                'copyright_date': cr.get('copyright_date', ''),
                'copyright_description': cr.get(
                    'copyright_description', ''
                ),
            })
        return filesets

    def search(self, query, limit, page, sort_by=None, books=None):
        result = get_default_dbt_client().search(
            self.fileset_id, query,
            limit=limit, page=page,
            sort_by=sort_by, books=books,
        )
        raw_verses = result.get('verses') or {}
        verse_items = raw_verses.get('data') or []
        normalized = [
            normalize_dbt_verse(v)
            for v in verse_items
        ]
        # Pagination lives in result['meta'] (documented schema)
        # or result['verses']['meta'] (observed b4.dbt.io
        # behaviour). Normalise to a clean { pagination: { ... } }
        # shape and strip internal DBT 'links' so callers only
        # ever use our own API to paginate. The pagination object
        # is always emitted — fields the provider omitted fall
        # back to locally-known values rather than dropping the
        # whole ``meta`` to ``{}``.
        raw_meta = (
            result.get('meta')
            or raw_verses.get('meta')
            or {}
        )
        pagination = raw_meta.get('pagination') or {}
        total = _fallback(pagination.get('total'), len(normalized))
        total_pages = pagination.get('total_pages')
        if total_pages is None:
            total_pages = (total + limit - 1) // limit if limit else 1
        meta = {
            'pagination': {
                'total': total,
                'count': _fallback(
                    pagination.get('count'), len(normalized)
                ),
                'per_page': _fallback(
                    pagination.get('per_page'), limit
                ),
                'current_page': _fallback(
                    pagination.get('current_page'), page
                ),
                'total_pages': total_pages,
            }
        }
        return {'verses': normalized, 'meta': meta}

    def search_grouped(self, query, books=None, sort_by=None):
        verses, total, truncated = fetch_dbt_matches(
            get_default_dbt_client(),
            self.fileset_id, query, books,
            sort_by=sort_by,
        )
        return {
            'groups': group_verses_by_book(verses),
            'meta': {'total': total, 'truncated': truncated},
        }

    def translation_listing(self, language_iso=None):
        """Raw DBT ``/bibles`` rows — the caller folds them
        through ``TranslationService._process_translations``.

        Note: the listing is truncated at ``limit=500`` rows
        (first page only). DBT hosts more bibles than that, so
        long-tail translations never reach the endpoint —
        accepted for now because the frontend's selector only
        needs the well-known set; paging the full catalog would
        multiply upstream calls on an anonymous endpoint."""
        params = {'limit': 500}
        if language_iso:
            params['language_code'] = language_iso.upper()
        response = get_default_dbt_client().get_bibles(**params)
        return response.get('data', [])


# ---------------------------------------------------------------------------
# Passage shaping — the fetch/shape seam moved out of
# ``BiblePassageSerializer.to_representation`` (the serializer
# remains the request-validation boundary; this function is the
# production fetch path and is what the view calls on
# ``serializer.validated_data``).
# ---------------------------------------------------------------------------


def _select_verses(rows, verse_numbers):
    """Filter ``[{'verse': n, 'text': ...}]`` rows to the
    requested verse numbers (``None`` keeps every row)."""
    if not verse_numbers:
        return rows
    wanted = set(verse_numbers)
    return [v for v in rows if v['verse'] in wanted]


def _book_not_in_fileset_body(book_id, book_name, chapter,
                              fileset_id, error=None):
    """Error body for a passage the fileset does not cover.

    ``BiblePassageView`` turns any body carrying
    ``error_code == 'book_not_in_fileset'`` into HTTP 404 so
    clients can deterministically fall back to another fileset.
    Field names follow the ``error``/``error_code`` contract used
    for provider failures. ``error`` overrides the default
    message when the caller knows a more specific cause.
    """
    return {
        'book': book_id,
        'book_name': book_name,
        'chapter': chapter,
        'error': error or (
            f"Book {book_id} chapter {chapter} is not available "
            f"in fileset {fileset_id}"
        ),
        'error_code': 'book_not_in_fileset',
    }


def get_passage_body(data):
    """Fetch + shape the passage response body.

    ``data`` is a validated ``BiblePassageSerializer`` dict:
    ``book``/``book_name``/``chapter``/``fileset_id``, optional
    ``verses`` selector and ``response_format`` (with the legacy
    ``format`` key still honoured for direct callers).

    Returns a plain dict — either the passage body or an error
    body carrying ``error``/``error_code``. Every provider's
    "fileset lacks this book/chapter" outcome (an upstream HTTP
    404, a ``PassageNotFoundError``, a successful-but-empty
    chapter, or a verse selector outside the chapter) normalizes
    to the single ``book_not_in_fileset`` code the frontend's
    fileset fallback keys on.
    """
    book_id = data.get('book')
    book_name = data.get('book_name', '')
    chapter = int(data.get('chapter'))
    fileset_id = data.get('fileset_id')
    # Optional verse selection (e.g. 'John 3:16-18'). Providers
    # that support narrowing get min/max as a hint to limit how
    # much they fetch upstream.
    verse_numbers = data.get('verses')
    verse_start = min(verse_numbers) if verse_numbers else None
    verse_end = max(verse_numbers) if verse_numbers else None
    # The view passes this as ``response_format`` to avoid
    # shadowing DRF's own ``format`` kwarg on APIView. Fall back
    # to the old ``format`` key for backwards compatibility.
    response_format = data.get(
        'response_format', data.get('format', 'text')
    )

    try:
        payload = provider_for(fileset_id).get_passage(
            book_id, chapter, response_format,
            verse_start=verse_start, verse_end=verse_end,
        )
        body = {
            'book': book_id,
            'book_name': book_name,
            'chapter': chapter,
            'format': payload['format'],
        }
        if payload['format'] == 'audio':
            body['audio_url'] = payload.get('audio_url')
            for key in ('duration_seconds', 'file_size_bytes'):
                if key in payload:
                    body[key] = payload[key]
            if payload.get('meta'):
                body['meta'] = payload['meta']
            return body

        selected = _select_verses(
            [
                {
                    'verse': v['verse_start'],
                    'text': v['verse_text'],
                }
                for v in payload['verses']
            ],
            verse_numbers,
        )
        # A successful-but-empty chapter (or a verse selection
        # outside it) is missing content — the fileset does not
        # cover the request, not a provider failure.
        if not selected:
            raise _no_verses(book_id, chapter, fileset_id)
        body['verses'] = selected
        if 'headings' in payload:
            headings = payload['headings']
            if verse_numbers:
                wanted = set(verse_numbers)
                headings = [
                    h for h in headings
                    if h.get('before_verse') in wanted
                ]
            body['headings'] = headings
        if payload.get('meta'):
            body['meta'] = payload['meta']
        return body
    except PassageNotFoundError as e:
        # Raised by every provider for "the fileset lacks this
        # book/chapter/selection" — one stable coverage-miss
        # code so clients can fall back to another fileset.
        return _book_not_in_fileset_body(
            book_id, book_name, chapter, fileset_id,
            error=getattr(e, 'client_message', None),
        )
    except Exception as e:
        logger.exception(f"Error fetching Bible passage: {e}")
        # An upstream HTTP 404 that escaped a provider branch
        # is also a coverage miss — normalize to
        # book_not_in_fileset. ``upstream_status_code``
        # covers every status carrier: ``exc.status`` (DBT
        # ApiException), ``exc.response.status_code``
        # (requests HTTPError) and ``exc.code`` (the
        # google.api_core exceptions GCS raises, e.g. a
        # missing timestamps artifact in the SWORD audio
        # path above).
        if upstream_status_code(e) == 404:
            return _book_not_in_fileset_body(
                book_id, book_name, chapter, fileset_id
            )
        return {
            'book': book_id,
            'book_name': book_name,
            'chapter': chapter,
            **provider_error_fields(e),
        }
