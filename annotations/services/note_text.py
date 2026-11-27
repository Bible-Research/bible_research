"""Plain-text extraction from stored ``Note.note_text``.

``note_text`` stores serialized TipTap JSON (legacy notes may
hold plain text). Search and ranking need a clean text form —
matching the raw JSON would hit markup keys and produce
garbage snippets. Ported from the frontend's
``toPlainText``/``docToPlainText`` in
``src/utils/tiptapContent.ts`` — keep the two in sync.
"""
import json

# Nodes that produce a line break after their content.
_BLOCK_TYPES = frozenset({
    'doc',
    'paragraph',
    'heading',
    'blockquote',
    'bulletList',
    'orderedList',
    'listItem',
    'codeBlock',
    'horizontalRule',
})

# Comment/note bodies are user-controlled JSON — cap the walk
# so a crafted doc (deep nesting, huge node counts) can't
# exhaust the stack. Same bounds as the frontend walker.
_MAX_WALK_DEPTH = 64
_MAX_WALK_NODES = 5000


def _is_tiptap_doc(doc):
    """True when *doc* is a parsed serialized TipTap doc."""
    return (
        isinstance(doc, dict)
        and doc.get('type') == 'doc'
        and isinstance(doc.get('content'), list)
    )


def note_text_to_plain(value):
    """Return the plain-text form of a stored ``note_text``.

    TipTap doc JSON is walked: ``text`` nodes concatenate, one
    ``'\\n'`` per block node, ``hardBreak`` emits a newline,
    non-text atoms (scripture refs/verse embeds) contribute
    nothing. Non-JSON legacy values pass through unchanged.
    """
    if not value:
        return ''
    try:
        doc = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not _is_tiptap_doc(doc):
        return value

    parts = []
    remaining = [_MAX_WALK_NODES]

    def _collect(node, depth):
        if remaining[0] <= 0 or depth > _MAX_WALK_DEPTH:
            return
        if not isinstance(node, dict):
            return
        remaining[0] -= 1
        node_type = node.get('type')
        if node_type == 'text':
            text = node.get('text')
            if isinstance(text, str):
                parts.append(text)
            return
        if node_type == 'hardBreak':
            parts.append('\n')
            return
        content = node.get('content')
        if isinstance(content, list):
            for child in content:
                _collect(child, depth + 1)
        if node_type in _BLOCK_TYPES:
            parts.append('\n')

    _collect(doc, 0)
    # The frontend strips only trailing newlines
    # (``.replace(/\n+$/, '')``) — match it exactly.
    return ''.join(parts).rstrip('\n')
