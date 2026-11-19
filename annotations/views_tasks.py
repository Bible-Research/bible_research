"""Cloud Tasks worker endpoints (OIDC-verified, non-user)."""
import logging

from rest_framework import status as drf_status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services.tasks import (
    OUTCOME_NOOP,
    OUTCOME_TOO_LONG,
    OUTCOME_TRANSLATED,
    TranslationError,
    run_translation_task,
    verify_task_oidc,
)

logger = logging.getLogger(__name__)

def _unauthorized():
    """401 in the error/error_code shape — never a shared
    Response instance (DRF mutates it on render)."""
    return Response(
        {'error': 'unauthorized', 'error_code': 'unauthorized'},
        status=drf_status.HTTP_401_UNAUTHORIZED,
    )


class TranslateNoteTaskView(APIView):
    """POST /api/v1/tasks/translate-note/ — Cloud Tasks worker.

    ``AllowAny`` at the DRF layer: authentication is the OIDC
    Bearer token Cloud Tasks attaches, verified inside the view
    against ``TASKS_INVOKER_SA_EMAIL`` — the ONLY credential the
    endpoint accepts.

    Body ``{note_id, source_hash}``:

    - stale *source_hash* (a newer edit won) → 204, a no-op so
      superseded runs can never clobber fresher rows (D4).
    - note over the char cap → 204 (rows marked ``failed``/
      ``too_long``; retrying a deterministic cap is pointless).
    - retryable failure → 500 → Cloud Tasks retries (max 5,
      exponential backoff).
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if verify_task_oidc(request) is None:
            return _unauthorized()

        data = request.data if isinstance(request.data, dict) \
            else {}
        note_id = data.get('note_id')
        source_hash = data.get('source_hash')
        if not note_id or not source_hash:
            return Response(
                {
                    'error': 'missing_parameter',
                    'error_code': 'missing_parameter',
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        try:
            outcome = run_translation_task(note_id, source_hash)
        except TranslationError:
            logger.exception(
                'translate-note task failed for note %s', note_id
            )
            return Response(
                {
                    'error': 'provider_error',
                    'error_code': 'provider_error',
                },
                status=drf_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if outcome in (OUTCOME_NOOP, OUTCOME_TOO_LONG):
            return Response(status=drf_status.HTTP_204_NO_CONTENT)
        if outcome == OUTCOME_TRANSLATED:
            return Response({'status': 'ok'})
        return _unauthorized()  # unreachable defensive default
