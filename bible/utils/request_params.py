"""Charset/length validators for ids that route to providers.

``fileset_id``, ``bible_id`` and friends are interpolated into
upstream URL paths (DBT ``v4_*`` calls) or select a provider at
routing time. Validating them at the view boundary keeps garbage
out of the DBT branch and out of upstream URLs.
"""
import re

# Fileset ids are routing keys: DBT ids (``ENGESVN2DA``,
# ``LATBSLN1DA-opus16``), synthetic registry ids
# (``LVSGLU8C1DA``, ``ENGNIV_API``) and listing abbrs
# (``GLU8``, ``NIV``).
FILESET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# ``bible_id`` on the copyright endpoint takes the same id space
# (fileset ids, registry ids and abbrs all resolve upstream).
BIBLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ISO 639-2/639-3 language codes are 2-3 letters (``eng``, ``lvs``).
LANGUAGE_ISO_RE = re.compile(r"^[a-zA-Z]{2,3}$")

# Book names like ``2 Chronicles`` or ``Song of Solomon`` — the
# ``book`` params resolved through ``get_dbt_book_id``.
BOOK_NAME_RE = re.compile(r"^[A-Za-z0-9 .'\-]{1,60}$")

# USFM book ids are 3 alphanumeric chars (``GEN``, ``1SA``).
USFM_BOOK_ID_RE = re.compile(r"^[A-Za-z0-9]{3}$")


def is_valid_fileset_id(value):
    """True when ``value`` is a plausible fileset id."""
    return bool(FILESET_ID_RE.match(value or ""))


def is_valid_bible_id(value):
    """True when ``value`` is a plausible bible/fileset id."""
    return bool(BIBLE_ID_RE.match(value or ""))


def is_valid_language_iso(value):
    """True when ``value`` is a 2-3 letter ISO language code."""
    return bool(LANGUAGE_ISO_RE.match(value or ""))


def is_valid_book_name(value):
    """True when ``value`` is a plausible Bible book name."""
    return bool(BOOK_NAME_RE.match(value or ""))


def is_valid_book_id_list(value):
    """True when ``value`` is a comma-separated list of 3-char
    USFM book ids (``JHN,ROM``) — the search ``books`` param."""
    if not value:
        return False
    return all(
        USFM_BOOK_ID_RE.match(token.strip())
        for token in value.split(',')
    )
