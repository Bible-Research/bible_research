"""Tests for bible.services.fileset_groups.

Covers the grouping algorithm from
``.research/translation-selector/implementation-plan.md``: codec
splitting, (type, digit) audio grouping, size-based coverage,
merge precedence, and SWORD/ESV singleton options.
"""
import pytest

from bible.services.fileset_groups import group_filesets


def _fs(fs_id, fs_type='audio', size='C'):
    return {'id': fs_id, 'type': fs_type, 'size': size}


def _translation(filesets, abbr='TST'):
    return {'abbr': abbr, 'filesets': filesets}


@pytest.mark.parametrize(
    'size,testaments,partial',
    [
        ('C', {'OT', 'NT'}, False),
        ('NT', {'NT'}, False),
        ('OT', {'OT'}, False),
        ('NTP', {'NT'}, True),
        ('OTP', {'OT'}, True),
        ('NTPOTP', {'OT', 'NT'}, True),
    ],
)
@pytest.mark.parametrize(
    'digit,kind', [('1', 'audio'), ('2', 'audio_drama')]
)
@pytest.mark.parametrize('opus16', [False, True])
def test_audio_grouping_matrix(
    size, testaments, partial, digit, kind, opus16
):
    """Coverage comes from ``size``; kind from the id digit."""
    # The N letter in the id is deliberately constant: the
    # fileset's coverage must come from ``size``, not the letter.
    fs_id = f'TSTXN{digit}DA' + ('-opus16' if opus16 else '')
    result = group_filesets(_translation([_fs(fs_id, size=size)]))

    assert result['text_options'] == []
    options = result['audio_options']
    assert len(options) == 1
    option = options[0]
    assert option['kind'] == kind
    assert option['id'] == f'TST:{kind}:1'
    assert option['partial'] == {
        t: partial and t in testaments for t in ('OT', 'NT')
    }
    codec = 'opus16' if opus16 else 'mp3'
    assert set(option['by_testament'].keys()) == testaments
    for testament in testaments:
        assert option['by_testament'][testament] == {
            codec: fs_id
        }


def test_opus16_variants_merge_with_mp3_siblings():
    """mp3 and -opus16 ids pair under one option."""
    result = group_filesets(_translation([
        _fs('LAVN1DA', size='NT'),
        _fs('LAVN1DA-opus16', size='NT'),
    ]))
    option = result['audio_options'][0]
    assert option['by_testament'] == {
        'NT': {'mp3': 'LAVN1DA', 'opus16': 'LAVN1DA-opus16'}
    }
    assert option['coverage_label'] == 'New Testament only'


def test_plain_and_drama_become_separate_options():
    """LAVNLI-style: digit 1 plain + digit 2 drama groups."""
    result = group_filesets(_translation([
        _fs('LATBSLP1DA', size='OTP'),
        _fs('LATBSLP1DA-opus16', size='OTP'),
        _fs('LATBSLN1DA', size='NT'),
        _fs('LATBSLN1DA-opus16', size='NT'),
        _fs('LATBSLP2DA', size='OTP'),
        _fs('LATBSLN2DA', size='NT'),
    ], abbr='LAVNLI'))

    options = result['audio_options']
    assert [o['kind'] for o in options] == ['audio', 'audio_drama']
    assert [o['id'] for o in options] == [
        'LAVNLI:audio:1', 'LAVNLI:audio_drama:1'
    ]

    plain = options[0]
    assert plain['by_testament'] == {
        'OT': {
            'mp3': 'LATBSLP1DA',
            'opus16': 'LATBSLP1DA-opus16',
        },
        'NT': {
            'mp3': 'LATBSLN1DA',
            'opus16': 'LATBSLN1DA-opus16',
        },
    }
    assert plain['partial'] == {'OT': True, 'NT': False}
    assert plain['coverage_label'] == 'Full Bible (partial)'

    drama = options[1]
    assert drama['by_testament']['NT'] == {'mp3': 'LATBSLN2DA'}


def test_mixed_id_prefixes_group_within_translation():
    """EN1WEB-style: fileset prefix != translation abbr."""
    result = group_filesets(_translation([
        _fs('EN1WEBO1DA', size='OT'),
        _fs('EN1WEBN1DA', size='NT'),
        _fs('EN1WEBO1DA-opus16', size='OT'),
        _fs('EN1WEBN1DA-opus16', size='NT'),
    ], abbr='WWH'))

    options = result['audio_options']
    assert len(options) == 1
    option = options[0]
    assert option['id'] == 'WWH:audio:1'
    assert option['by_testament']['OT'] == {
        'mp3': 'EN1WEBO1DA', 'opus16': 'EN1WEBO1DA-opus16'
    }
    assert option['by_testament']['NT'] == {
        'mp3': 'EN1WEBN1DA', 'opus16': 'EN1WEBN1DA-opus16'
    }
    assert option['coverage_label'] == 'Full Bible'


def test_complete_member_beats_partial_for_same_testament():
    """An NT fileset wins over an NTP one claiming NT."""
    result = group_filesets(_translation([
        _fs('TSTP1DA', size='NTP'),
        _fs('TSTN1DA', size='NT'),
    ]))
    option = result['audio_options'][0]
    assert option['by_testament']['NT'] == {'mp3': 'TSTN1DA'}
    assert option['partial']['NT'] is False


def test_non_p_lettered_id_beats_p_lettered_id():
    """With equal partial flags, non-``P`` id letters win."""
    result = group_filesets(_translation([
        _fs('TSTP1DA', size='NTP'),
        _fs('TSTN1DA', size='NTP'),
    ]))
    option = result['audio_options'][0]
    assert option['by_testament']['NT'] == {'mp3': 'TSTN1DA'}


def test_partial_both_testaments_coverage():
    """AUSWBT-style NTPOTP fileset covers both, both partial."""
    result = group_filesets(_translation([
        _fs('AUSWBTP1DA', size='NTPOTP'),
    ], abbr='AUSWBT'))
    option = result['audio_options'][0]
    assert option['by_testament'] == {
        'OT': {'mp3': 'AUSWBTP1DA'},
        'NT': {'mp3': 'AUSWBTP1DA'},
    }
    assert option['partial'] == {'OT': True, 'NT': True}
    assert option['coverage_label'] == 'Full Bible (partial)'


def test_sword_audio_is_generated_singleton():
    """LVSGLU8C1DA matches the DA suffix but is SWORD TTS."""
    result = group_filesets(_translation([
        _fs('LVSGLU8', 'text_plain', 'C'),
        _fs('LVSGLU8C1DA', 'audio', 'C'),
    ], abbr='GLU8'))

    audio = result['audio_options']
    assert len(audio) == 1
    assert audio[0]['kind'] == 'generated'
    assert audio[0]['id'] == 'GLU8:generated:1'
    assert audio[0]['by_testament'] == {
        'OT': {'generated': 'LVSGLU8C1DA'},
        'NT': {'generated': 'LVSGLU8C1DA'},
    }
    assert audio[0]['coverage_label'] == 'Full Bible'

    text = result['text_options']
    assert len(text) == 1
    assert text[0]['kind'] == 'text'
    assert text[0]['by_testament']['NT'] == {'text': 'LVSGLU8'}


def test_esv_fileset_is_audio_singleton():
    """ENGESV_API does not match the DA suffix pattern."""
    result = group_filesets(_translation([
        _fs('ENGESV_API', 'text_plain', 'C'),
        _fs('ENGESV_API', 'audio', 'C'),
    ], abbr='ESV'))

    audio = result['audio_options']
    assert len(audio) == 1
    assert audio[0]['kind'] == 'audio'
    assert audio[0]['by_testament'] == {
        'OT': {'api': 'ENGESV_API'},
        'NT': {'api': 'ENGESV_API'},
    }


def test_unmatched_audio_id_becomes_own_option():
    """A non-registry, non-DA-suffixed id is a singleton."""
    result = group_filesets(_translation([
        _fs('SOMETHINGODD', 'audio', 'NT'),
    ]))
    option = result['audio_options'][0]
    assert option['kind'] == 'audio'
    assert option['by_testament'] == {
        'NT': {'mp3': 'SOMETHINGODD'}
    }


def test_text_plain_filesets_merge_into_single_option():
    """_ET splits merge; non-plain text types are ignored."""
    result = group_filesets(_translation([
        _fs('ENGKJV', 'text_usx', 'C'),
        _fs('ENGKJVO_ET', 'text_plain', 'OT'),
        _fs('ENGKJVN_ET', 'text_plain', 'NT'),
        _fs('ENGKJV', 'text_format', 'C'),
    ], abbr='ENGKJV'))

    text = result['text_options']
    assert len(text) == 1
    option = text[0]
    assert option['id'] == 'ENGKJV:text:1'
    assert option['by_testament'] == {
        'OT': {'text': 'ENGKJVO_ET'},
        'NT': {'text': 'ENGKJVN_ET'},
    }
    assert option['coverage_label'] == 'Full Bible'


def test_audio_only_translation_has_no_text_option():
    result = group_filesets(_translation([
        _fs('LATLVRN1DA', size='NT'),
    ], abbr='LAVLVR'))
    assert result['text_options'] == []
    assert len(result['audio_options']) == 1


def test_empty_filesets_yield_empty_options():
    result = group_filesets({'abbr': 'XXX', 'filesets': []})
    assert result == {'text_options': [], 'audio_options': []}
