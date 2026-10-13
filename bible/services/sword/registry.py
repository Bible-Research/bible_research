"""Registry of locally-bundled SWORD Bible modules served via pysword."""
from pathlib import Path

# Directory holding the vendored CrossWire raw-zip modules.
SWORD_MODULES_DIR = Path(__file__).resolve().parents[2] / "sword_modules"

# Map of synthetic fileset_id -> module metadata.
# fileset_id convention: 3-letter ISO 639-3 language code + short abbr,
# matching the DBT id style (e.g. ENGESV) so existing clients don't need
# to special-case routing.
SWORD_TRANSLATIONS = {
    "LVSGLU8": {
        "module_name": "LvGluck8",
        "zip_filename": "LvGluck8.zip",
        "name": "Latvian Glück 8th edition",
        "abbr": "GLU8",
        "language": "Latvian",
        "language_iso": "lvs",  # ISO 639-3
        "license": "Public Domain",
        # Audio fileset ids follow DBT convention:
        #   {base_id}{size}{version}{type}
        #   C = Complete, N = version, DA = Digital Audio
        # C1DA serves artifacts stitched from per-verse TTS calls
        # (generation mode "verse"); C2DA serves whole-chapter TTS
        # artifacts with SSML-mark verse timestamps (mode "chapter").
        "audio_fileset_id": "LVSGLU8C1DA",
        "audio_fileset_id_chapter": "LVSGLU8C2DA",
    },
}


def canonical_sword_fileset_id(fileset_id: str) -> str | None:
    """Return the registry key (e.g. LVSGLU8) for a fileset id,
    audio fileset id (e.g. LVSGLU8C1DA), or abbr (e.g. GLU8)."""
    if not fileset_id:
        return None
    key = fileset_id.strip().upper()
    if key in SWORD_TRANSLATIONS:
        return key
    for sfid, meta in SWORD_TRANSLATIONS.items():
        if meta["abbr"].upper() == key:
            return sfid
        if meta.get("audio_fileset_id", "").upper() == key:
            return sfid
        if meta.get("audio_fileset_id_chapter", "").upper() == key:
            return sfid
    return None


def audio_mode_for_fileset(fileset_id: str) -> str:
    """Return the GCS audio generation mode a fileset id reads from:
    ``"chapter"`` for ``audio_fileset_id_chapter`` ids (e.g.
    LVSGLU8C2DA), ``"verse"`` for everything else. Unknown ids default
    to ``"verse"`` — the historical layout."""
    key = (fileset_id or "").strip().upper()
    for meta in SWORD_TRANSLATIONS.values():
        if meta.get("audio_fileset_id_chapter", "").upper() == key:
            return "chapter"
    return "verse"


def is_sword_fileset(fileset_id: str) -> bool:
    return canonical_sword_fileset_id(fileset_id) is not None


def get_sword_meta(fileset_id: str) -> dict:
    canon = canonical_sword_fileset_id(fileset_id)
    if canon is None:
        raise KeyError(fileset_id)
    return SWORD_TRANSLATIONS[canon]
