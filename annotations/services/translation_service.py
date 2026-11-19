"""Cloud Translation service for note text.

Structural TipTap translation (D5): a serialized TipTap doc is
walked, every ``type:'text'`` node value is batched into ONE
``translate_text`` call per target language, and results are
spliced back in order — marks (bold, links) and non-text atoms
(``scriptureRef``/``mention``) survive untouched. ``@USFM.C[.V[-E]]``
tokens stored as plain text are masked before the call and
restored on splice-back so MT cannot mangle them into text the
frontend's ``scriptureLinkify`` no longer matches. Non-doc
strings translate as a single plain-text segment.

Everything here is language-agnostic on purpose — ``Comment``
rows can reuse ``translate_texts`` unchanged later.
"""
import hashlib
import json
import logging
import re

from django.conf import settings

from bible.services.storage import gcs

logger = logging.getLogger(__name__)

# Python port of the frontend's SCRIPTURE_TOKEN_REGEX
# (reactive-bible src/utils/scriptureLinkify.tsx): ``@`` tokens
# not preceded by a word char — covers '@JHN.3.16' and
# hand-typed colon variants like '@JHN.3:16'. ``re`` supports
# the same fixed-width lookbehind.
SCRIPTURE_TOKEN_REGEX = re.compile(r'(?<!\w)(@[a-zA-Z0-9_+.:-]+)')

# Mask placeholder: unpronounceable in every language so NMT
# passes it through. The restore regex tolerates inserted spaces
# and case folding, which MT occasionally applies to unknown
# tokens.
_PLACEHOLDER_FMT = 'ZXZX{index}XZXZ'
_PLACEHOLDER_RE = re.compile(
    r'zxzx\s*(\d+)\s*xzxz', re.IGNORECASE
)

# Engine → v3 model resource id. ``google-nmt`` is the default
# general model; future engines (google-llm, gemini-…) map or
# pass through verbatim.
_ENGINE_MODELS = {
    'google-nmt': 'general/nmt',
    'google-llm': 'general/translation-llm',
}

# GCS monthly usage counter id (D11) — shares the audio bucket's
# ``state/usage/<id>/<YYYY-MM>.json`` layout via
# ``increment_monthly_usage``.
USAGE_COUNTER_ID = 'note-translate'


class TranslationError(Exception):
    """Retryable Cloud Translation failure (API error, network)."""


class NoteTooLongError(TranslationError):
    """Deterministic per-note cap breach — never retried."""

    def __init__(self, chars, limit):
        super().__init__(
            f'note text is {chars} chars, cap is {limit}'
        )
        self.chars = chars
        self.limit = limit


def hash_note_text(note_text):
    """sha256 hex of *note_text* — the idempotency key for runs."""
    return hashlib.sha256(
        (note_text or '').encode('utf-8')
    ).hexdigest()


def is_tiptap_doc_string(value):
    """True when *value* is a serialized TipTap doc.

    Port of the frontend ``isTiptapDocString``
    (src/utils/tiptapContent.ts): JSON object with
    ``type == 'doc'`` and a ``content`` array.
    """
    try:
        doc = json.loads(value)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(doc, dict)
        and doc.get('type') == 'doc'
        and isinstance(doc.get('content'), list)
    )


def mask_tokens(text):
    """Mask ``@USFM…`` tokens in *text* with placeholders.

    Returns ``(masked_text, tokens)`` where *tokens* is the
    ordered list of originals removed. Hand-typed colon variants
    are covered because the matcher is the frontend's
    SCRIPTURE_TOKEN_REGEX, not a strict ref parser.
    """
    tokens = []

    def _sub(match):
        tokens.append(match.group(1))
        return _PLACEHOLDER_FMT.format(index=len(tokens) - 1)

    return SCRIPTURE_TOKEN_REGEX.sub(_sub, text), tokens


def restore_tokens(text, tokens):
    """Splice *tokens* back into a translated masked string.

    A placeholder MT dropped entirely leaves the translated
    segment as-is (logged) rather than corrupting indexes.
    """
    def _sub(match):
        index = int(match.group(1))
        if index < len(tokens):
            return tokens[index]
        return match.group(0)

    restored = _PLACEHOLDER_RE.sub(_sub, text)
    if len(tokens) and len(_PLACEHOLDER_RE.findall(text)) < len(
        tokens
    ):
        logger.warning(
            'translation returned %d placeholder(s), expected %d',
            len(_PLACEHOLDER_RE.findall(text)),
            len(tokens),
        )
    return restored


def _collect_text_nodes(node, out):
    """Append every ``type:'text'`` dict in *node* to *out*.

    Collected by reference so splice-back mutates the parsed doc
    in place; non-text children (scriptureRef/mention atoms) are
    descended but never translated.
    """
    if not isinstance(node, dict):
        return
    if node.get('type') == 'text' and isinstance(
        node.get('text'), str
    ):
        out.append(node)
        return
    content = node.get('content')
    if isinstance(content, list):
        for child in content:
            _collect_text_nodes(child, out)


def _client():
    """Build the TranslationServiceClient lazily.

    Local dev sits behind an ALPN-stripping TLS proxy, so gRPC
    is unusable there — REST transport keeps the zero-infra
    fallback working (same workaround as the other Google
    clients; prod keeps the default gRPC transport).
    """
    from google.cloud import translate_v3

    kwargs = (
        {}
        if getattr(settings, 'IS_GCP_ENVIRONMENT', False)
        else {'transport': 'rest'}
    )
    return translate_v3.TranslationServiceClient(**kwargs)


def _parent():
    project = getattr(settings, 'GOOGLE_CLOUD_PROJECT', None) or \
        'bible-research-489314'
    return f'projects/{project}/locations/global'


def _model_path():
    """v3 ``model`` id for the configured engine, '' for default."""
    engine = getattr(settings, 'NOTE_TRANSLATION_ENGINE', '')
    model = _ENGINE_MODELS.get(engine, engine)
    if not model:
        return ''
    project = getattr(settings, 'GOOGLE_CLOUD_PROJECT', None) or \
        'bible-research-489314'
    return f'projects/{project}/locations/global/models/{model}'


def _record_usage(chars, target_language):
    """Bill *chars* to the monthly GCS counter; log per language.

    Best-effort: a counter failure (missing bucket/creds locally)
    must never break translation — it only skews the spend audit.
    """
    logger.info(
        'note-translate billed %d chars into %s',
        chars, target_language,
    )
    try:
        gcs.increment_monthly_usage(USAGE_COUNTER_ID, chars)
    except Exception:
        logger.exception(
            'note-translate usage increment failed (%d chars)',
            chars,
        )


def _translate_segments(contents, target_language, source_language):
    """One ``translate_text`` call for *contents*.

    Returns ``(translated_texts, detected_language)``; detected is
    ``None`` when *source_language* was supplied. The batch keeps
    the whole note in ONE call — order is preserved by the API
    and per-segment billing is identical.
    """
    request = {
        'parent': _parent(),
        'contents': contents,
        'mime_type': 'text/plain',
        'target_language_code': target_language,
    }
    if source_language:
        request['source_language_code'] = source_language
    model = _model_path()
    if model:
        request['model'] = model
    try:
        response = _client().translate_text(request=request)
    except Exception as exc:
        raise TranslationError(
            f'translate_text into {target_language} failed'
        ) from exc
    billed = sum(len(c) for c in contents)
    _record_usage(billed, target_language)
    detected = None
    if not source_language and response.translations:
        detected = (
            response.translations[0].detected_language_code or None
        )
    return (
        [t.translated_text for t in response.translations],
        detected,
    )


def translate_note_text(note_text, target_language,
                        source_language=None):
    """Translate one serialized note text into *target_language*.

    Returns ``(translated_text, detected_language)`` —
    *detected_language* comes free from the same ``translate_text``
    call (D6) and is ``None`` when *source_language* was passed.

    Raises ``NoteTooLongError`` over the configured char cap and
    ``TranslationError`` for retryable API failures.
    """
    if not (note_text or '').strip():
        # Nothing to translate or detect — skip the API call.
        return note_text, None
    limit = getattr(settings, 'NOTE_TRANSLATE_MAX_CHARS', 10000)
    if is_tiptap_doc_string(note_text):
        doc = json.loads(note_text)
        nodes = []
        _collect_text_nodes(doc, nodes)
        masked_segments = []
        token_maps = []
        total = 0
        for node in nodes:
            masked, tokens = mask_tokens(node['text'])
            masked_segments.append(masked)
            token_maps.append(tokens)
            total += len(masked)
        if total > limit:
            raise NoteTooLongError(total, limit)
        if not masked_segments:
            return note_text, None
        translated, detected = _translate_segments(
            masked_segments, target_language, source_language,
        )
        for node, segment, tokens in zip(
            nodes, translated, token_maps
        ):
            node['text'] = restore_tokens(segment, tokens)
        return json.dumps(doc, ensure_ascii=False), detected

    masked, tokens = mask_tokens(note_text)
    if len(masked) > limit:
        raise NoteTooLongError(len(masked), limit)
    translated, detected = _translate_segments(
        [masked], target_language, source_language,
    )
    return restore_tokens(translated[0], tokens), detected


def translate_texts(texts, targets, source_language=None):
    """Translate *texts* into every language in *targets*.

    Language-agnostic entry point (comments reuse it later).
    Returns ``(detected_source, {target: [translated, ...]})`` —
    the list is parallel to *texts*; a target equal to the
    detected/provided source is skipped (callers mark that row
    ``source`` instead of paying for an identity translation).

    The first call omits ``source_language_code`` so detection
    costs nothing extra; subsequent calls reuse it.
    """
    results = {}
    detected = source_language
    for target in targets:
        if detected is not None and target == detected:
            continue
        translated = []
        for text in texts:
            out, det = translate_note_text(
                text, target, source_language=detected,
            )
            if detected is None:
                detected = det
                if target == detected:
                    # Detection coincided with this target — its
                    # row is the source slot, not a translation.
                    translated = []
                    break
            translated.append(out)
        if target == detected:
            continue
        results[target] = translated
    return detected, results
