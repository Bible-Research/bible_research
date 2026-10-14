"""Registry of API.Bible-backed filesets (api.scripture.api.bible).

Synthetic fileset ids mirror the ESV/SWORD convention so existing
callers route on ``fileset_id`` without knowing which upstream
provider serves the request. Every helper here works without an
``API_BIBLE_KEY`` configured — only the HTTP client needs the key.
"""
APIBIBLE_TRANSLATIONS = {
    "ENGNIV_API": {
        "name": "New International Version",
        "abbr": "NIV",
        "language": "English",
        "language_iso": "eng",
        # API.Bible bible id for the NIV 2011 text.
        "bible_id": "78a9f6124f344018-01",
        # API.Bible audio bible id ("The Listener's Bible").
        "audio_bible_id": "5dd1a42b02d2dee6-01",
        # Audio fileset id follows DBT convention:
        #   {base_id}{size}{version}{type}
        #   C = Complete, 1 = version, DA = Digital Audio
        "audio_fileset_id": "ENGNIVC1DA",
        "license": (
            "Copyright Biblica, Inc. Licensed via API.Bible "
            "non-commercial Starter plan."
        ),
    },
}


def canonical_apibible_fileset_id(fileset_id: str) -> "str | None":
    """Return the registry key for a text or audio fileset id.

    ``ENGNIVC1DA`` (audio) canonicalizes to ``ENGNIV_API`` the
    same way ``LVSGLU8C1DA`` canonicalizes to ``LVSGLU8`` for
    SWORD.
    """
    if not fileset_id:
        return None
    key = fileset_id.strip().upper()
    if key in APIBIBLE_TRANSLATIONS:
        return key
    for fid, meta in APIBIBLE_TRANSLATIONS.items():
        if meta.get("audio_fileset_id", "").upper() == key:
            return fid
    return None


def is_apibible_fileset(fileset_id: str) -> bool:
    return canonical_apibible_fileset_id(fileset_id) is not None


def get_apibible_meta(fileset_id: str) -> dict:
    canon = canonical_apibible_fileset_id(fileset_id)
    if canon is None:
        raise KeyError(fileset_id)
    return APIBIBLE_TRANSLATIONS[canon]


def get_apibible_translation_listing() -> list:
    """Return registry entries shaped like DBT/SWORD translations."""
    return [
        {
            "abbr": meta["abbr"],
            "name": meta["name"],
            "language": meta["language"],
            "iso": meta["language_iso"],
            "filesets": [
                {
                    "id": fid,
                    "type": "text_plain",
                    "size": "C",
                },
                {
                    "id": meta["audio_fileset_id"],
                    "type": "audio",
                    "size": "C",
                },
            ],
        }
        for fid, meta in APIBIBLE_TRANSLATIONS.items()
    ]
