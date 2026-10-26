"""Tests for bible.utils.bible_books book-name resolution."""

import pytest

from bible.utils.bible_books import get_dbt_book_id


@pytest.mark.parametrize('name, expected', [
    ('Isaiah', 'ISA'),
    ('isaiah', 'ISA'),
    ('  Isaiah  ', 'ISA'),
    ('2 Chronicles', '2CH'),
    ('Song of Solomon', 'SNG'),
    ('1 John', '1JN'),
])
def test_exact_names(name, expected):
    assert get_dbt_book_id(name) == expected


@pytest.mark.parametrize('name, expected', [
    # The misspelling reported in the issue
    ('Isiah', 'ISA'),
    ('issiah', 'ISA'),
    ('isaih', 'ISA'),
    # Other common misspellings / typos
    ('genisis', 'GEN'),
    ('deutoronomy', 'DEU'),
    ('ezekial', 'EZK'),
    ('philipians', 'PHP'),
    ('revelations', 'REV'),
    ('psalm', 'PSA'),
    ('hebrew', 'HEB'),
    ('jhon', 'JHN'),
    ('exedus', 'EXO'),
    ('jud', 'JUD'),
])
def test_common_misspellings(name, expected):
    assert get_dbt_book_id(name) == expected


@pytest.mark.parametrize('name, expected', [
    ('I Samuel', '1SA'),
    ('ii corinthians', '2CO'),
    ('1st Kings', '1KI'),
    ('2nd Peter', '2PE'),
    ('Third John', '3JN'),
    ('Revelation of John', 'REV'),
    ('Song of Songs', 'SNG'),
    ('Canticles', 'SNG'),
    ('Apocalypse', 'REV'),
])
def test_alternate_spellings_and_titles(name, expected):
    assert get_dbt_book_id(name) == expected


@pytest.mark.parametrize('name, expected', [
    # OSIS book abbreviations — resolved by exact alias, never
    # by fuzzy guessing (passage #9).
    ('Gen', 'GEN'),
    ('Exod', 'EXO'),
    ('Lev', 'LEV'),
    ('Deut', 'DEU'),
    ('Judg', 'JDG'),
    ('1Sam', '1SA'),
    ('2 Kgs', '2KI'),
    ('1Chr', '1CH'),
    ('2 Chr', '2CH'),
    ('Neh', 'NEH'),
    ('Esth', 'EST'),
    ('Ps', 'PSA'),
    ('Prov', 'PRO'),
    ('Eccl', 'ECC'),
    ('Song', 'SNG'),
    ('Isa', 'ISA'),
    ('Jer', 'JER'),
    ('Lam', 'LAM'),
    ('Ezek', 'EZK'),
    ('Hos', 'HOS'),
    ('Obad', 'OBA'),
    ('Mic', 'MIC'),
    ('Zeph', 'ZEP'),
    ('Hag', 'HAG'),
    ('Zech', 'ZEC'),
    ('Matt', 'MAT'),
    ('Rom', 'ROM'),
    ('1Cor', '1CO'),
    ('2 Cor', '2CO'),
    ('Eph', 'EPH'),
    ('Phil', 'PHP'),
    ('Col', 'COL'),
    ('1Thess', '1TH'),
    ('Phlm', 'PHM'),
    ('Heb', 'HEB'),
    ('Jas', 'JAS'),
    ('1Pet', '1PE'),
    ('3John', '3JN'),
    ('Rev', 'REV'),
])
def test_osis_abbreviations(name, expected):
    assert get_dbt_book_id(name) == expected


@pytest.mark.parametrize('name', [
    'xyz',
    'not a book',
    '',
    # Ambiguous without a number must not guess a numbered book
    'samuel',
    'kings',
    'corinthians',
    'peter',
    # No such numbered book exists — must not fall back to a
    # different number ('3 samuel' must not return '2 samuel')
    '3 samuel',
    '4 john',
    # Abbreviations outside the OSIS set are not resolved
    'eze',
    'mat',
    # 'jud' is deliberately not an OSIS alias (ambiguous
    # between Judges and Jude), but fuzzy matching still
    # resolves it to Jude — see test_common_misspellings.
])
def test_unresolvable_names_return_none(name):
    assert get_dbt_book_id(name) is None
