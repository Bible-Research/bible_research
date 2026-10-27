"""In-process LRU cache for whole-chapter verse payloads.

``provider_for`` wraps every text-capable provider except
API.Bible in ``_CachingProvider``, which routes ``get_chapter``
and ``get_passage(format='text')`` through this module so
repeated passage requests and note enrichment stop paying an
upstream call per chapter. The App Engine deployment is a
single process (``env: standard``, ``max_instances: 1``,
gunicorn without ``--workers``), so a module-level
``OrderedDict`` + ``threading.Lock`` is coherent — the same
pattern as ``_signed_url_cache`` in
``bible/services/storage/gcs.py``.

License notes (docs/plans/verse-chapter-caching-plan.md):

- Public-domain filesets have no storage cap — they are bounded
  only by the global LRU (``PUBLIC_DOMAIN_FILESETS``).
- Copyrighted translations are capped by a per-translation
  verse budget. The cap travels with the copyrighted work, not
  the transport: ESV text through DBT (``ENGESV``) and through
  api.esv.org (``ENGESV_API``) share one ``esv`` budget.
- ``ApiBibleProvider`` is excluded upstream in ``provider_for``
  — its FUMS ``meta`` token must be reported per display — so
  this module never sees it.

Cached payloads are served as stored — callers must not mutate
the returned dict (all current consumers only read).
"""
import threading
import time
from collections import OrderedDict

from bible.services.esv.registry import canonical_esv_fileset_id
from bible.services.sword.registry import (
    canonical_sword_fileset_id,
)
from bible.utils.provider_errors import PassageNotFoundError

# Text is near-immutable but does receive occasional upstream
# corrections; the TTL bounds staleness without invalidation.
TTL_SECONDS = 24 * 60 * 60
# ``PassageNotFoundError`` is cached briefly so the frontend's
# fileset-fallback probing cannot re-penalize upstream on every
# note list or passage request.
NEGATIVE_TTL_SECONDS = 5 * 60
# ~4-6 KB per parsed chapter -> ~3 MB worst case on F1's 256 MB.
MAX_ENTRIES = 500
# Per-translation-group verse budget matching Crossway's
# 500-verse ESV cap; public-domain filesets are exempt.
VERSE_BUDGET = 500

# Filesets whose texts are public domain — no licensor storage
# cap applies.
PUBLIC_DOMAIN_FILESETS = frozenset({'ENGKJV', 'LVSGLU8'})

# Fileset-id prefix -> the copyrighted work it serves. The verse
# budget is charged per work across every transport.
_GROUP_PREFIXES = (
    ('ENGESV', 'esv'),
    ('ENGNIV', 'niv'),
)

# ``(canonical_fileset_id, book_id, chapter)`` ->
# ``(value, expires_at_monotonic, group, verse_count)`` where
# ``value`` is the normalized chapter payload or, for a negative
# entry, the ``PassageNotFoundError`` to re-raise.
_cache: OrderedDict = OrderedDict()
_lock = threading.Lock()


def canonical_fileset_id(fileset_id):
    """Return the registry-canonical fileset id for cache keying.

    SWORD abbrs and audio ids (``GLU8``, ``LVSGLU8C1DA``)
    collapse to the text module id so every alias shares one
    entry; ESV ids canonicalize through their registry; DBT ids
    have no registry and are uppercased so ``engkjv`` and
    ``ENGKJV`` share one entry and budget group.
    """
    canon = canonical_sword_fileset_id(fileset_id)
    if canon is not None:
        return canon
    canon = canonical_esv_fileset_id(fileset_id)
    if canon is not None:
        return canon
    return fileset_id.upper() if fileset_id else fileset_id


def cache_translation_group(fileset_id):
    """Return the copyrighted-work group a fileset draws its
    verse budget from, or ``None`` for public-domain filesets.

    ``ENGESV*`` filesets share ``esv``; ``ENGNIV*`` share
    ``niv``; every other id is its own group so unenumerated
    licensors still get the conservative cap.
    """
    canon = canonical_fileset_id(fileset_id)
    if not canon or canon in PUBLIC_DOMAIN_FILESETS:
        return None
    for prefix, group in _GROUP_PREFIXES:
        if canon.startswith(prefix):
            return group
    return canon


def get_or_fetch(fileset_id, book_id, chapter, fetch):
    """Return the chapter payload, fetching upstream on a miss.

    ``fetch()`` is called with no arguments when no live entry
    exists; its return value is cached under the canonical
    ``(fileset, book, chapter)`` key for ``TTL_SECONDS``. A
    ``PassageNotFoundError`` from ``fetch`` is negative-cached
    for ``NEGATIVE_TTL_SECONDS`` and re-raised on later hits;
    every other exception propagates uncached so
    ``rate_limited``/``provider_error`` failures still reach
    ``provider_error_fields`` and Sentry.
    """
    key = (
        canonical_fileset_id(fileset_id), book_id, int(chapter)
    )
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            if entry[1] > now:
                _cache.move_to_end(key)
                value = entry[0]
                if isinstance(value, PassageNotFoundError):
                    raise value
                return value
            del _cache[key]
    # fetch() runs outside the lock on purpose: holding it
    # across a ~1s upstream call would serialize every request,
    # while a concurrent cold miss only costs a duplicate fetch.
    try:
        payload = fetch()
    except PassageNotFoundError as exc:
        _store(key, exc, NEGATIVE_TTL_SECONDS)
        raise
    _store(key, payload, TTL_SECONDS)
    return payload


def clear():
    """Drop every cached entry (pytest isolation hook)."""
    with _lock:
        _cache.clear()


def _store(key, value, ttl):
    """Insert ``value`` under ``key`` and enforce the bounds."""
    group = cache_translation_group(key[0])
    verse_count = (
        len(value.get('verses') or [])
        if isinstance(value, dict) else 0
    )
    with _lock:
        _cache[key] = (
            value, time.monotonic() + ttl, group, verse_count
        )
        _cache.move_to_end(key)
        _enforce_group_budget(group)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)


def _enforce_group_budget(group):
    """Evict ``group``'s oldest entries while its cached verse
    total exceeds ``VERSE_BUDGET``. Caller holds ``_lock``;
    ``None`` (public domain) skips the budget entirely."""
    if group is None:
        return
    total = sum(
        entry[3] for entry in _cache.values()
        if entry[2] == group
    )
    for key, entry in list(_cache.items()):
        if total <= VERSE_BUDGET:
            break
        if entry[2] == group:
            total -= entry[3]
            del _cache[key]
