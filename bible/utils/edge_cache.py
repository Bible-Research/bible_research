"""Vercel edge-cache opt-in for anonymous public GET endpoints.

The reactive-bible frontend proxies ``/api/*`` through Vercel's
CDN, which caches external-rewrite responses only where the
origin emits ``Vercel-CDN-Cache-Control``. Vercel strips that
header before the browser, so it steers the edge alone and
never relaxes the browser-facing ``Cache-Control`` posture —
licensed content stays uncacheable client-side.

Only catalog endpoints whose response is identical for every
anonymous caller opt in (translations, languages, copyright).
Verse text/audio payloads are licensed content and must not
sit in third-party caches; user-scoped endpoints vary per
caller. Requests carrying ``Authorization`` bypass the Vercel
cache regardless.
"""

EDGE_CACHE_HEADER = 'Vercel-CDN-Cache-Control'

# Catalog listings (translations, languages): the merged
# registry + upstream catalog changes on deploy or a rare
# upstream addition, so a day-fresh entry with a week of
# stale-while-revalidate grace keeps misses cheap without
# serving meaningfully stale data.
EDGE_CACHE_CATALOG_DIRECTIVE = (
    'max-age=86400, stale-while-revalidate=604800'
)

# Copyright statements are static per bible id (registry
# license metadata or the upstream statement) and already
# carry ``Cache-Control: public`` for browsers — the edge can
# hold them for a week with a day of revalidation grace.
EDGE_CACHE_STATIC_DIRECTIVE = (
    'max-age=604800, stale-while-revalidate=86400'
)


class VercelEdgeCacheMixin:
    """Emit ``Vercel-CDN-Cache-Control`` on safe GET responses.

    Mix in before ``APIView`` and set ``edge_cache_directive``
    to the desired header value. The header is added only to
    2xx responses whose ``Cache-Control`` does not already
    forbid downstream caching, so client errors, provider
    failures and degraded ``no-store`` bodies are never marked.
    """

    edge_cache_directive = ''

    def finalize_response(self, request, response, *args,
                          **kwargs):
        response = super().finalize_response(
            request, response, *args, **kwargs
        )
        cache_control = response.headers.get('Cache-Control', '')
        if (
            self.edge_cache_directive
            and request.method == 'GET'
            and 200 <= response.status_code < 300
            and 'no-store' not in cache_control
            and 'private' not in cache_control
        ):
            response[EDGE_CACHE_HEADER] = self.edge_cache_directive
        return response
