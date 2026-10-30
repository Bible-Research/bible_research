"""Registry of DBT audio filesets whose verse timestamps we
generate ourselves with Cloud Speech-to-Text.

DBT serves no timestamps for these filesets (verified live:
``GET /timestamps/{fs}/{book}/{ch}`` returns ``{"data": []}``),
so the ``generate_audio_timestamps`` job produces
``timestamps_generated/`` artifacts in the audio bucket and
``DbtProvider.get_timestamps`` falls back to them when DBT
answers empty (or fails).

Each descriptor:

- ``reference`` — fileset id of the reference text the STT
  transcript is aligned against (``LVSGLU8`` = the Glück SWORD
  module; the 1953 LVR recording is a Glück revision and
  LAVNLI has no text in our stack, so GLU8 is the fallback for
  both).
- ``min_alignment_score`` — optional per-fileset override of
  ``aligner.MIN_ALIGNMENT_SCORE``. LATBSLP* is the LAVNLI 2012
  edition, which drifts further from Glück wording than LVR
  1953 does, so its flagging threshold is lower.
"""

GENERATED_TIMESTAMPS_FILESETS = {
    "LATLVRN1DA": {"reference": "LVSGLU8"},
    "LATLVRP1DA": {"reference": "LVSGLU8"},
    "LATBSLP1DA": {
        "reference": "LVSGLU8",
        "min_alignment_score": 0.3,
    },
    "LATBSLP2DA": {
        "reference": "LVSGLU8",
        "min_alignment_score": 0.3,
    },
}

# DBT opus/webm variants serve the same recordings as their mp3
# base fileset — the frontend strips this suffix before asking
# for timestamps (``resolveTimestampsFilesetId``), and the
# serving fallback does the same so generated artifacts apply
# to both ids.
_OPUS16_SUFFIX = "-OPUS16"


def generated_timestamps_base_id(fileset_id: str) -> str | None:
    """Return the registered base fileset id for ``fileset_id`` —
    uppercased, ``-opus16`` suffix stripped — or ``None`` when it
    is not a generated-timestamps fileset."""
    if not fileset_id:
        return None
    key = fileset_id.strip().upper()
    if key.endswith(_OPUS16_SUFFIX):
        key = key[: -len(_OPUS16_SUFFIX)]
    if key in GENERATED_TIMESTAMPS_FILESETS:
        return key
    return None


def generated_timestamps_meta(fileset_id: str) -> dict | None:
    """Return the registry descriptor for ``fileset_id`` (or
    ``None``). Callers needing the GCS key should use
    ``generated_timestamps_base_id``."""
    base = generated_timestamps_base_id(fileset_id)
    if base is None:
        return None
    return GENERATED_TIMESTAMPS_FILESETS[base]
