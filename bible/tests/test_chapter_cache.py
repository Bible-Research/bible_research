"""Tests for the in-process chapter cache.

Covers ``bible/services/chapter_cache.py`` and the
``_CachingProvider`` wrapper ``provider_for`` applies: cache
hits, canonical keying, the per-translation verse budget, the
LRU bound, TTL expiry, negative caching of
``PassageNotFoundError``, and the audio passthrough. The
conftest autouse fixture clears the cache between tests.
"""
from unittest.mock import MagicMock, patch

import pytest

from bible.services import chapter_cache
from bible.services.providers import (
    ApiBibleProvider,
    _CachingProvider,
    get_passage_body,
    provider_for,
)
from bible.utils.provider_errors import PassageNotFoundError


def _verses(*nums):
    return [
        {'verse_start': n, 'verse_text': f'text {n}'}
        for n in nums
    ]


def _payload(*verse_nums):
    """A normalized ``get_chapter`` payload."""
    return {
        'verses': _verses(*verse_nums),
        'headings': [],
        'meta': {},
    }


class _Fetch:
    """Counting stand-in for an upstream chapter fetch."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.payload


def _dbt_client(rows):
    client = MagicMock()
    client.get_verses.return_value = {'data': rows}
    return client


def _passage(fileset_id='ENGKJV', **extra):
    data = {
        'book': 'JHN',
        'book_name': 'John',
        'chapter': 3,
        'fileset_id': fileset_id,
    }
    data.update(extra)
    return data


def _fill(fileset_id, book_id, chapters, verse_count):
    """Cache ``verse_count``-verse payloads per chapter."""
    for chapter in chapters:
        chapter_cache.get_or_fetch(
            fileset_id, book_id, chapter,
            _Fetch(_payload(*range(1, verse_count + 1))),
        )


def test_text_providers_are_wrapped():
    for fileset_id in ('ENGESV_API', 'LVSGLU8', 'ENGKJV'):
        provider = provider_for(fileset_id)
        assert isinstance(provider, _CachingProvider)
        assert provider.fileset_id == fileset_id


def test_apibible_provider_is_not_wrapped():
    """API.Bible must report each response's FUMS ``meta``
    token per display, so its provider is never cached."""
    provider = provider_for('ENGNIV_API')
    assert isinstance(provider, ApiBibleProvider)
    assert not isinstance(provider, _CachingProvider)


def test_capability_metadata_delegates_to_inner():
    """``audio_codec``/``audio_kind`` are class attributes the
    ``__getattr__`` fallback cannot reach — they are delegated
    explicitly so fileset grouping still works."""
    assert provider_for('LVSGLU8').audio_codec == 'generated'
    assert provider_for('LVSGLU8').audio_kind == 'generated'
    assert provider_for('ENGESV_API').audio_codec == 'api'
    provider = provider_for('ENGKJV')
    assert provider.audio_codec is None
    assert provider.audio_kind == 'audio'


@patch('bible.services.providers.get_default_dbt_client')
def test_second_get_chapter_hits_cache(mock_get):
    mock_get.return_value = _dbt_client(_verses(1, 2, 3))

    provider = provider_for('ENGKJV')
    first = provider.get_chapter('JHN', 3)
    second = provider.get_chapter('JHN', 3)

    assert second == first
    mock_get.return_value.get_verses.assert_called_once_with(
        'JHN', 3, bible_id='ENGKJV'
    )


@patch('bible.services.providers.get_default_dbt_client')
def test_keys_miss_independently(mock_get):
    mock_get.return_value = _dbt_client(_verses(1, 2))

    provider = provider_for('ENGKJV')
    provider.get_chapter('JHN', 3)
    provider.get_chapter('JHN', 4)
    provider.get_chapter('GEN', 3)
    provider_for('ENGWEB').get_chapter('JHN', 3)

    assert mock_get.return_value.get_verses.call_count == 4


@patch('bible.services.providers.get_default_sword_client')
def test_sword_aliases_share_one_entry(mock_get):
    """``GLU8`` and ``LVSGLU8C1DA`` canonicalize to
    ``LVSGLU8`` — all aliases hit the same cache entry."""
    mock_get.return_value.get_chapter_verses.return_value = (
        _verses(1, 2)
    )

    provider_for('GLU8').get_chapter('JHN', 3)
    provider_for('LVSGLU8').get_chapter('JHN', 3)
    provider_for('LVSGLU8C1DA').get_chapter('JHN', 3)

    mock_get.return_value.get_chapter_verses\
        .assert_called_once_with('GLU8', 'JHN', 3)


@patch('bible.services.providers.get_default_esv_client')
def test_verse_selector_does_not_poison_chapter(mock_get):
    """A ranged request must not shrink the cached chapter:
    the miss fetches the whole chapter upstream (narrowing
    hints are never forwarded), so a later chapter-only
    request gets every verse from cache."""
    mock_get.return_value.get_chapter_with_headings\
        .return_value = {
            'verses': _verses(*range(1, 37)),
            'headings': [],
        }
    data = _passage(fileset_id='ENGESV_API')

    selected = get_passage_body({**data, 'verses': [16, 17, 18]})
    assert (
        [v['verse'] for v in selected['verses']] == [16, 17, 18]
    )

    whole = get_passage_body(data)
    assert len(whole['verses']) == 36

    # One upstream call, and it carried no verse-range hints.
    mock_get.return_value.get_chapter_with_headings\
        .assert_called_once_with('JHN', 3)


@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_text_passage_served_from_cache(mock_get):
    """``DbtProvider.get_passage`` fetches raw rows directly;
    through the wrapper a second text request hits the chapter
    cache instead of upstream."""
    mock_get.return_value = _dbt_client(_verses(1, 2))

    assert get_passage_body(_passage())['verses']
    assert get_passage_body(_passage())['verses']

    mock_get.return_value.get_verses.assert_called_once_with(
        'JHN', 3, bible_id='ENGKJV'
    )


@patch('bible.services.providers.get_default_dbt_client')
def test_audio_passage_never_cached(mock_get):
    """Audio payloads carry expiring upstream URLs — every
    audio request must reach the provider."""
    mock_get.return_value = _dbt_client([{
        'path': 'https://cdn.example.com/JHN3.mp3',
        'duration': 180.0,
        'filesize_in_bytes': 10,
    }])
    data = _passage(
        fileset_id='ENGNASN1DA', response_format='audio'
    )

    assert get_passage_body(data)['audio_url']
    assert get_passage_body(data)['audio_url']

    assert mock_get.return_value.get_verses.call_count == 2


@patch('bible.services.providers.get_default_dbt_client')
def test_not_found_is_negative_cached(mock_get):
    """A coverage miss is cached so fileset-fallback probing
    cannot re-penalize upstream on every request."""
    mock_get.return_value = _dbt_client([])
    provider = provider_for('ENGKJV')

    with pytest.raises(PassageNotFoundError):
        provider.get_chapter('JHN', 99)
    with pytest.raises(PassageNotFoundError):
        provider.get_chapter('JHN', 99)

    mock_get.return_value.get_verses.assert_called_once()


@patch('bible.services.providers.get_default_dbt_client')
def test_upstream_errors_are_never_cached(mock_get):
    """Only ``PassageNotFoundError`` is negative-cached —
    rate-limit/server failures must keep reaching callers (and
    Sentry) on every attempt."""
    mock_get.return_value.get_verses.side_effect = (
        ConnectionError('boom')
    )
    provider = provider_for('ENGKJV')

    for _ in range(2):
        with pytest.raises(ConnectionError):
            provider.get_chapter('JHN', 3)

    assert mock_get.return_value.get_verses.call_count == 2


def test_expired_entry_refetches():
    """An entry older than ``TTL_SECONDS`` is dropped and
    fetched again."""
    first = _Fetch(_payload(1))
    second = _Fetch(_payload(1))
    with patch('bible.services.chapter_cache.time') as mtime:
        mtime.monotonic.return_value = 0.0
        chapter_cache.get_or_fetch('ENGKJV', 'JHN', 3, first)
        mtime.monotonic.return_value = (
            chapter_cache.TTL_SECONDS + 1
        )
        chapter_cache.get_or_fetch('ENGKJV', 'JHN', 3, second)
    assert first.calls == 1
    assert second.calls == 1


def test_negative_entry_expires_quickly():
    """Negative entries use the short TTL — a passage that
    starts existing upstream becomes visible within minutes."""
    fetch = _Fetch(exc=PassageNotFoundError('no such chapter'))
    retry = _Fetch(_payload(1))
    with patch('bible.services.chapter_cache.time') as mtime:
        mtime.monotonic.return_value = 0.0
        with pytest.raises(PassageNotFoundError):
            chapter_cache.get_or_fetch('ENGKJV', 'JHN', 9, fetch)
        mtime.monotonic.return_value = (
            chapter_cache.NEGATIVE_TTL_SECONDS + 1
        )
        assert chapter_cache.get_or_fetch(
            'ENGKJV', 'JHN', 9, retry
        )['verses']
    assert fetch.calls == 1
    assert retry.calls == 1


def test_verse_budget_evicts_group_oldest():
    """Caching more than ``VERSE_BUDGET`` verses for one
    translation group evicts that group's oldest chapters."""
    _fill('ENGWEB', 'GEN', [1, 2, 3], 200)  # 600 > 480

    refetch = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('ENGWEB', 'GEN', 1, refetch)
    assert refetch.calls == 1

    still_cached = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('ENGWEB', 'GEN', 3, still_cached)
    assert still_cached.calls == 0


def test_esv_budget_shared_across_fileset_ids():
    """``ENGESV`` (DBT) and ``ENGESV_API`` draw from one shared
    ``esv`` budget — the cap follows the copyrighted work, not
    the transport."""
    assert (
        chapter_cache.cache_translation_group('ENGESV')
        == chapter_cache.cache_translation_group('ENGESV_API')
        == 'esv'
    )
    _fill('ENGESV', 'GEN', [1, 2], 200)
    _fill('ENGESV_API', 'GEN', [1], 200)  # 600 > 480

    refetch = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('ENGESV', 'GEN', 1, refetch)
    assert refetch.calls == 1


def test_public_domain_filesets_skip_the_budget():
    """``ENGKJV``/``LVSGLU8`` carry no storage cap — only the
    global LRU bounds them."""
    for fileset_id in ('ENGKJV', 'LVSGLU8', 'GLU8'):
        assert (
            chapter_cache.cache_translation_group(fileset_id)
            is None
        )
    _fill('ENGKJV', 'GEN', [1, 2, 3], 200)  # 600 > 480 anyway
    for chapter in (1, 2, 3):
        check = _Fetch(_payload(1))
        chapter_cache.get_or_fetch(
            'ENGKJV', 'GEN', chapter, check
        )
        assert check.calls == 0


def test_translation_group_mapping():
    """Every non-public-domain id gets a group: known
    copyrighted works collapse to one budget, unknown filesets
    each form their own so unenumerated licensors stay capped."""
    groups = chapter_cache.cache_translation_group
    assert groups('ENGNIV_API') == 'niv'
    assert groups('ENGNIVC1DA') == 'niv'
    assert groups('LATBSLV') == 'LATBSLV'


def test_lowercase_dbt_id_shares_entry_and_exemption():
    """Non-registry fileset ids key case-insensitively —
    ``engkjv`` hits the ``ENGKJV`` entry and keeps the
    public-domain budget exemption."""
    assert (
        chapter_cache.canonical_fileset_id('engkjv') == 'ENGKJV'
    )
    assert (
        chapter_cache.cache_translation_group('engkjv') is None
    )

    fetch = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('ENGKJV', 'JHN', 3, fetch)
    check = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('engkjv', 'JHN', 3, check)
    assert fetch.calls == 1
    assert check.calls == 0


def test_lru_bound_evicts_oldest_entry():
    """The global cap bounds memory: past ``MAX_ENTRIES``
    chapters the oldest entry goes."""
    _fill(
        'ENGKJV', 'GEN',
        range(1, chapter_cache.MAX_ENTRIES + 2), 1,
    )

    refetch = _Fetch(_payload(1))
    chapter_cache.get_or_fetch('ENGKJV', 'GEN', 1, refetch)
    assert refetch.calls == 1

    newest = _Fetch(_payload(1))
    chapter_cache.get_or_fetch(
        'ENGKJV', 'GEN', chapter_cache.MAX_ENTRIES + 1, newest
    )
    assert newest.calls == 0
