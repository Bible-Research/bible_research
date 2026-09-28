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
    # Abbreviations are not resolved
    'eze',
    'mat',
    'isa',
])
def test_unresolvable_names_return_none(name):
    assert get_dbt_book_id(name) is None
