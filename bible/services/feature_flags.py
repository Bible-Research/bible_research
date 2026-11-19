"""Database-backed feature flags (runtime kill switches).

Flags live in the ``FeatureFlag`` table and are toggled via
Django admin — no redeploy needed. Lookups are read-through-
cached with a short TTL so a check costs ~nothing, and a
**missing row means enabled**: features ship on and ops turns
them off when needed (kill-switch semantics, not dark launch).
"""
import logging

from django.core.cache import cache

from bible.models import FeatureFlag

logger = logging.getLogger(__name__)

# The SWORD grouped/paged search scan kill switch.
SWORD_SEARCH_FLAG_KEY = 'sword_search'

# Short enough for ops to see a toggle take effect quickly;
# long enough that a per-request flag check never hits the DB
# in practice.
_FLAG_CACHE_TTL_SECONDS = 30


def _flag_cache_key(key):
    return f'feature_flag:{key}'


def is_feature_enabled(key):
    """Return whether the ``key`` feature flag is enabled.

    Read-through cache: a hit costs no DB query, a miss reads
    the flag row and caches the boolean for
    ``_FLAG_CACHE_TTL_SECONDS``. A missing row means enabled —
    kill-switch semantics. An unreadable table (e.g. the
    migration has not been applied yet) also fails open rather
    than 500ing every gated request.
    """
    cache_key = _flag_cache_key(key)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        enabled = FeatureFlag.objects.filter(
            key=key
        ).values_list('enabled', flat=True).first()
    except Exception:
        logger.exception(
            'FeatureFlag lookup failed for key=%s; '
            'defaulting to enabled.',
            key,
        )
        return True
    enabled = True if enabled is None else bool(enabled)
    cache.set(cache_key, enabled, _FLAG_CACHE_TTL_SECONDS)
    return enabled


def is_sword_search_enabled():
    """Whether SWORD-fileset search is allowed to run."""
    return is_feature_enabled(SWORD_SEARCH_FLAG_KEY)


def invalidate_feature_flag(key):
    """Drop a flag's cached value so a toggle takes effect
    immediately (wired to admin save/delete)."""
    cache.delete(_flag_cache_key(key))
