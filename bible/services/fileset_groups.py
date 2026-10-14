"""Group a translation's raw filesets into normalized options.

The translations endpoint exposes provider filesets verbatim
(``{id, type, size}``), which forces clients to understand DBT id
grammar (``-opus16`` codec suffixes, testament splits, the
plain/drama version digit). This module folds each translation's
fileset list into two normalized collections:

* ``text_options`` — at most one option merging every
  ``text_plain`` fileset into a testament → fileset id map.
* ``audio_options`` — one option per listening experience (plain
  reading, dramatized, generated voice), each with a
  testament → codec → concrete fileset id map.

Option shape::

    {
        "id": "LAVNLI:audio:1",     # synthetic: {abbr}:{kind}:{n}
        "kind": "audio",            # audio | audio_drama |
                                    # generated | text
        "by_testament": {
            "OT": {"mp3": "LATBSLP1DA",
                   "opus16": "LATBSLP1DA-opus16"},
            "NT": {"mp3": "LATBSLN1DA",
                   "opus16": "LATBSLN1DA-opus16"},
        },
        "coverage_label": "Full Bible (partial)",
        "partial": {"OT": True, "NT": False},
    }

Grouping rules (see
``.research/translation-selector/implementation-plan.md``):

1. Ids ending ``-opus16`` are codec variants of the stripped base
   id; everything else is ``mp3``.
2. Audio filesets group by ``(type, digit)`` where the digit comes
   from the trailing ``[CONPS]\\dDA`` id suffix — ``1`` is a plain
   reading, ``2`` is dramatized. Filesets that do not match the
   suffix become singleton options: SWORD TTS ids get kind
   ``generated``, ``ENGESV_API`` and other unmatched ids get kind
   ``audio``.
3. Coverage comes from ``size``, not the id letter: ``C`` → OT+NT;
   else containing ``NT`` → NT, containing ``OT`` → OT; ``P``
   anywhere marks the covered testaments partial.
4. When two group members claim the same testament prefer complete
   over partial, and a non-``P`` lettered id over a ``P`` one.
   Precedence is applied per codec slot, so ``partial`` and
   ``coverage_label`` reflect the winning member of each slot —
   a partial ``opus16`` winner marks the testament partial even
   when a complete ``mp3`` sibling exists (``opus16`` is the
   preferred codec).
5. Text grouping keeps only ``type == 'text_plain'`` (``text_usx``
   /``text_json``/``text_format`` are ignored) and merges all of
   them into a single ``by_testament`` map.

``by_testament`` codec keys are an open set, not an enum: DBT
members use ``mp3``/``opus16``, SWORD TTS uses ``generated``,
ESV uses ``api`` and merged text options use ``text``. Filesets
whose ``size`` yields no recognizable testament coverage are
skipped so options never advertise a dead testament map.
"""
import re

from .esv.registry import is_esv_fileset
from .sword.registry import is_sword_fileset

OPUS16_SUFFIX = '-opus16'

# DBT audio ids end ``{size_letter}{version_digit}DA`` where the
# digit is 1 for a plain reading and 2 for a dramatized one.
AUDIO_ID_RE = re.compile(r'([CONPS])(\d)DA$')

AUDIO_TYPES = frozenset({'audio', 'audio_drama'})
TEXT_TYPE = 'text_plain'

TESTAMENTS = ('OT', 'NT')


def _split_codec(fileset_id):
    """Return ``(base_id, codec)`` splitting off ``-opus16``."""
    if fileset_id.endswith(OPUS16_SUFFIX):
        return fileset_id[:-len(OPUS16_SUFFIX)], 'opus16'
    return fileset_id, 'mp3'


def _size_coverage(size):
    """Return ``(testaments, partial)`` for a DBT ``size`` code.

    ``C`` covers both testaments; otherwise ``NT``/``OT``
    substrings mark coverage and ``P`` anywhere marks the covered
    testaments as partial (``NTPOTP`` → both, partial).
    """
    size = (size or '').upper()
    testaments = set()
    if 'C' in size:
        testaments.update(TESTAMENTS)
    if 'NT' in size:
        testaments.add('NT')
    if 'OT' in size:
        testaments.add('OT')
    return testaments, 'P' in size


def _member_rank(member):
    """Rank for testament-slot precedence; lower wins.

    Complete filesets beat partial ones; ids whose trailing size
    letter is not ``P`` beat ``P``-lettered ids.
    """
    return (
        1 if member['partial'] else 0,
        1 if member['letter'] == 'P' else 0,
    )


def _coverage_label(by_testament, members_by_testament):
    """Human-readable coverage such as ``New Testament only``."""
    covered = [t for t in TESTAMENTS if t in by_testament]
    if len(covered) == 2:
        label = 'Full Bible'
    elif covered == ['NT']:
        label = 'New Testament only'
    elif covered == ['OT']:
        label = 'Old Testament only'
    else:
        return 'No coverage'
    is_partial = any(
        member['partial']
        for t in covered
        for member in members_by_testament[t].values()
    )
    if is_partial:
        label += ' (partial)'
    return label


def _build_option(option_id, kind, members):
    """Merge group members into one normalized option dict."""
    by_testament = {}
    members_by_testament = {}
    for member in members:
        for testament in TESTAMENTS:
            if testament not in member['testaments']:
                continue
            codecs = by_testament.setdefault(testament, {})
            codec_members = members_by_testament.setdefault(
                testament, {}
            )
            current = codec_members.get(member['codec'])
            if (
                current is None
                or _member_rank(member) < _member_rank(current)
            ):
                codecs[member['codec']] = member['id']
                codec_members[member['codec']] = member

    partial = {
        t: any(
            m['partial']
            for m in members_by_testament.get(t, {}).values()
        )
        for t in TESTAMENTS
    }
    return {
        'id': option_id,
        'kind': kind,
        'by_testament': {
            t: by_testament[t]
            for t in TESTAMENTS
            if t in by_testament
        },
        'coverage_label': _coverage_label(
            by_testament, members_by_testament
        ),
        'partial': partial,
    }


def _make_member(fileset, codec=None):
    """Extract grouping-relevant fields from a raw fileset."""
    fs_id = fileset['id']
    base_id, split_codec = _split_codec(fs_id)
    testaments, partial = _size_coverage(fileset.get('size'))
    match = AUDIO_ID_RE.search(base_id)
    return {
        'id': fs_id,
        'base_id': base_id,
        'codec': codec if codec is not None else split_codec,
        'testaments': testaments,
        'partial': partial,
        'letter': match.group(1) if match else None,
        'digit': match.group(2) if match else None,
    }


def group_filesets(translation):
    """Compute ``text_options``/``audio_options`` for a translation.

    Args:
        translation: Mapping with ``abbr`` and a ``filesets`` list
            of ``{id, type, size}`` dicts.

    Returns:
        ``{'text_options': [...], 'audio_options': [...]}`` —
        ``text_options`` holds at most one option (kind ``text``);
        ``audio_options`` holds one option per grouped or singleton
        audio fileset.
    """
    abbr = translation.get('abbr') or ''
    filesets = translation.get('filesets') or []

    audio_groups = {}
    # Pending entries preserve encounter order so option ordering
    # is stable across identical inputs.
    pending = []
    text_members = []

    for fileset in filesets:
        if not fileset.get('id'):
            continue
        fs_type = fileset.get('type') or ''
        member = _make_member(fileset)
        if not member['testaments']:
            # A missing/unrecognized ``size`` yields no coverage —
            # grouping it would produce an option that can never
            # resolve to a concrete fileset.
            continue

        if fs_type == TEXT_TYPE:
            member['codec'] = 'text'
            text_members.append(member)
            continue
        if fs_type not in AUDIO_TYPES:
            continue

        if is_sword_fileset(member['base_id']):
            # SWORD audio is generated TTS served from our own
            # storage, not a DBT codec variant.
            member['codec'] = 'generated'
            pending.append(('single', member, 'generated'))
        elif is_esv_fileset(member['base_id']):
            member['codec'] = 'api'
            pending.append(('single', member, 'audio'))
        elif member['digit'] is not None:
            key = (fs_type, member['digit'])
            if key not in audio_groups:
                audio_groups[key] = []
                pending.append(('group', key))
            audio_groups[key].append(member)
        else:
            pending.append(('single', member, 'audio'))

    audio_options = []
    kind_counts = {}
    for item in pending:
        if item[0] == 'group':
            fs_type, digit = item[1]
            members = audio_groups[item[1]]
            if digit == '2' or fs_type == 'audio_drama':
                kind = 'audio_drama'
            else:
                kind = 'audio'
        else:
            _, member, kind = item
            members = [member]
        n = kind_counts.get(kind, 0) + 1
        kind_counts[kind] = n
        audio_options.append(
            _build_option(f'{abbr}:{kind}:{n}', kind, members)
        )

    text_options = []
    if text_members:
        text_options.append(
            _build_option(f'{abbr}:text:1', 'text', text_members)
        )

    return {
        'text_options': text_options,
        'audio_options': audio_options,
    }
