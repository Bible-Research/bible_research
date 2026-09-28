"""Utility functions for Bible books and standard abbreviations."""

import difflib
import re

# Book data: (book_name, book_code, testament)
# Testament: 'OT' = Old Testament, 'NT' = New Testament
_BIBLE_BOOKS = [
    # Old Testament
    ('genesis', 'GEN', 'OT'), ('exodus', 'EXO', 'OT'),
    ('leviticus', 'LEV', 'OT'), ('numbers', 'NUM', 'OT'),
    ('deuteronomy', 'DEU', 'OT'), ('joshua', 'JOS', 'OT'),
    ('judges', 'JDG', 'OT'), ('ruth', 'RUT', 'OT'),
    ('1 samuel', '1SA', 'OT'), ('2 samuel', '2SA', 'OT'),
    ('1 kings', '1KI', 'OT'), ('2 kings', '2KI', 'OT'),
    ('1 chronicles', '1CH', 'OT'), ('2 chronicles', '2CH', 'OT'),
    ('ezra', 'EZR', 'OT'), ('nehemiah', 'NEH', 'OT'),
    ('esther', 'EST', 'OT'), ('job', 'JOB', 'OT'),
    ('psalms', 'PSA', 'OT'), ('proverbs', 'PRO', 'OT'),
    ('ecclesiastes', 'ECC', 'OT'), ('song of solomon', 'SNG', 'OT'),
    ('isaiah', 'ISA', 'OT'), ('jeremiah', 'JER', 'OT'),
    ('lamentations', 'LAM', 'OT'), ('ezekiel', 'EZK', 'OT'),
    ('daniel', 'DAN', 'OT'), ('hosea', 'HOS', 'OT'),
    ('joel', 'JOL', 'OT'), ('amos', 'AMO', 'OT'),
    ('obadiah', 'OBA', 'OT'), ('jonah', 'JON', 'OT'),
    ('micah', 'MIC', 'OT'), ('nahum', 'NAM', 'OT'),
    ('habakkuk', 'HAB', 'OT'), ('zephaniah', 'ZEP', 'OT'),
    ('haggai', 'HAG', 'OT'), ('zechariah', 'ZEC', 'OT'),
    ('malachi', 'MAL', 'OT'),
    # New Testament
    ('matthew', 'MAT', 'NT'), ('mark', 'MRK', 'NT'),
    ('luke', 'LUK', 'NT'), ('john', 'JHN', 'NT'),
    ('acts', 'ACT', 'NT'), ('romans', 'ROM', 'NT'),
    ('1 corinthians', '1CO', 'NT'), ('2 corinthians', '2CO', 'NT'),
    ('galatians', 'GAL', 'NT'), ('ephesians', 'EPH', 'NT'),
    ('philippians', 'PHP', 'NT'), ('colossians', 'COL', 'NT'),
    ('1 thessalonians', '1TH', 'NT'),
    ('2 thessalonians', '2TH', 'NT'),
    ('1 timothy', '1TI', 'NT'), ('2 timothy', '2TI', 'NT'),
    ('titus', 'TIT', 'NT'), ('philemon', 'PHM', 'NT'),
    ('hebrews', 'HEB', 'NT'), ('james', 'JAS', 'NT'),
    ('1 peter', '1PE', 'NT'), ('2 peter', '2PE', 'NT'),
    ('1 john', '1JN', 'NT'), ('2 john', '2JN', 'NT'),
    ('3 john', '3JN', 'NT'), ('jude', 'JUD', 'NT'),
    ('revelation', 'REV', 'NT'),
]

# Generate derived data structures from the single source
DBT_BOOK_NAME_TO_ID = {name: code for name, code, _ in _BIBLE_BOOKS}
BOOK_CODE_TO_TESTAMENT = {
    code: testament for _, code, testament in _BIBLE_BOOKS
}
OLD_TESTAMENT_BOOKS = {
    code for _, code, t in _BIBLE_BOOKS if t == 'OT'
}
NEW_TESTAMENT_BOOKS = {
    code for _, code, t in _BIBLE_BOOKS if t == 'NT'
}


_LOWERCASE_WORDS = frozenset({'of', 'and', 'the', 'a', 'an'})


def _book_title(name: str) -> str:
    """Title-case a book name, keeping prepositions lower."""
    parts = name.split()
    return ' '.join(
        w.lower() if (i > 0 and w.lower() in _LOWERCASE_WORDS)
        else w.capitalize()
        for i, w in enumerate(parts)
    )


_BOOK_ID_TO_NAME = {
    code: _book_title(name) for name, code, _ in _BIBLE_BOOKS
}

# Book order mapping for sorting (1-66)
BOOK_ORDER_MAP = {
    _book_title(name): idx + 1
    for idx, (name, _, _) in enumerate(_BIBLE_BOOKS)
}


def get_book_name_from_id(book_id: str) -> str:
    """Return the full English book name for a DBT book ID.

    Args:
        book_id (str): DBT book ID (e.g. 'MAT', 'GEN', '1KI')

    Returns:
        str: Title-cased book name (e.g. 'Matthew', 'Genesis',
             '1 Kings')

    Raises:
        ValueError: If the book_id is not found.
    """
    name = _BOOK_ID_TO_NAME.get(book_id.upper())
    if name is None:
        raise ValueError(f"Unknown book ID: {book_id!r}")
    return name


_ROMAN_TO_ARABIC = {'i': '1', 'ii': '2', 'iii': '3'}


def normalize_sword_book_name(name: str) -> str:
    """Normalise a SWORD module book name to the canonical
    lowercase form used in ``_BIBLE_BOOKS``.

    Handles Roman-numeral prefixes (``I Samuel`` → ``1 samuel``)
    and alternate names (``Revelation of John`` → ``revelation``).
    """
    normalized = ' '.join(name.lower().strip().split())
    parts = normalized.split()
    if parts and parts[0] in _ROMAN_TO_ARABIC:
        parts[0] = _ROMAN_TO_ARABIC[parts[0]]
    normalized = ' '.join(parts)
    if normalized == 'revelation of john':
        normalized = 'revelation'
    return normalized


# Alternate titles and frequent misspellings resolved explicitly so
# they never depend on (or get stolen by) fuzzy matching — e.g.
# 'canticles' is closer to '1/2 chronicles' than to its real target.
_BOOK_ALIASES = {
    'isiah': 'isaiah',
    'issiah': 'isaiah',
    'isaih': 'isaiah',
    'jhon': 'john',
    'song of songs': 'song of solomon',
    'canticles': 'song of solomon',
    'apocalypse': 'revelation',
}

_NUMBER_WORDS = {'first': '1', 'second': '2', 'third': '3'}

_ORDINAL_RE = re.compile(r'\b([123])(?:st|nd|rd|th)\b')

# Minimum similarity for fuzzy book-name matching. 0.8 catches
# single-character typos in longer names ('genisis' -> 'genesis')
# without guessing on unrelated input.
_FUZZY_CUTOFF = 0.8


def _normalize_for_lookup(book_name):
    """Normalise free-text book input for lookup.

    Builds on ``normalize_sword_book_name`` (lowercase, whitespace,
    Roman numerals, 'revelation of john') and additionally handles
    ordinal suffixes ('1st Kings') and number words
    ('First Samuel').
    """
    normalized = normalize_sword_book_name(book_name)
    normalized = _ORDINAL_RE.sub(r'\1', normalized)
    first, _, rest = normalized.partition(' ')
    if first in _NUMBER_WORDS:
        normalized = ' '.join(
            p for p in [_NUMBER_WORDS[first], rest] if p
        )
    return normalized


def _fuzzy_book_id(normalized):
    """Fuzzy-match a normalised book name to a DBT book ID.

    A leading digit locks matching to books with that number so
    e.g. '3 samuel' does not resolve to '2 samuel'; unnumbered
    input only matches unnumbered books so 'peter' stays
    ambiguous instead of guessing '2 peter'.
    """
    number, _, rest = normalized.partition(' ')
    if number.isdigit() and rest:
        candidates = {
            name.split(' ', 1)[1]: code
            for name, code in DBT_BOOK_NAME_TO_ID.items()
            if name.startswith(f'{number} ')
        }
    else:
        rest = normalized
        candidates = {
            name: code
            for name, code in DBT_BOOK_NAME_TO_ID.items()
            if not name[0].isdigit()
        }
    matches = difflib.get_close_matches(
        rest, list(candidates), n=1, cutoff=_FUZZY_CUTOFF
    )
    if matches:
        return candidates[matches[0]]
    return None


def get_dbt_book_id(book_name):
    """Convert a book name to its standard book ID.

    Args:
        book_name (str): The name of the book (e.g., '2 chronicles')

    Returns:
        str: The standard book ID (e.g., '2CH'), or None if not found

    Matching is tolerant: after normalisation it tries an exact
    match, then common misspellings/alternate titles ('isiah',
    'song of songs'), then a fuzzy match so minor typos like
    'genisis' still resolve.
    """
    normalized = _normalize_for_lookup(book_name)
    book_id = DBT_BOOK_NAME_TO_ID.get(normalized)
    if book_id is None:
        book_id = DBT_BOOK_NAME_TO_ID.get(
            _BOOK_ALIASES.get(normalized, '')
        )
    if book_id is None:
        book_id = _fuzzy_book_id(normalized)
    return book_id


def get_testament(book_id):
    """Determine which testament a book belongs to.

    Args:
        book_id (str): The DBT book ID (e.g., '2CH', 'MAT')

    Returns:
        str: 'OT' for Old Testament, 'NT' for New Testament,
             or None if book not found
    """
    return BOOK_CODE_TO_TESTAMENT.get(book_id.upper())


def get_audio_bible_id(
    book_id,
    base_translation='ENGESV',
    audio_type='2DA',
    codec='opus16'
):
    """Get the appropriate audio Bible ID based on testament.

    Args:
        book_id (str): The DBT book ID (e.g., '2CH', 'MAT')
        base_translation (str): Base translation code
                                (default: 'ENGESV')
        audio_type (str): Audio type code (default: '1DA')
        codec (str): Audio codec (default: 'opus16')

    Returns:
        str: The complete audio Bible ID
             (e.g., 'ENGESVO1DA-opus16' for OT books,
                  'ENGESVN1DA-opus16' for NT books)
    """
    testament = get_testament(book_id)
    if testament is None:
        # Default to OT if unknown
        testament = 'OT'

    # Convert testament to single letter for API
    # (API uses 'O' for Old Testament, 'N' for New Testament)
    testament_letter = testament[0]  # 'OT' -> 'O', 'NT' -> 'N'

    # Build the audio Bible ID
    audio_id = (
        f"{base_translation}{testament_letter}{audio_type}"
    )
    if codec:
        audio_id = f"{audio_id}-{codec}"

    return audio_id
