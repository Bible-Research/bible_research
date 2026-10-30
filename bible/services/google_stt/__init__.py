"""Cloud Speech-to-Text (V1) helpers for generating verse
timestamps for DBT audio filesets DBT has no timestamps for.

Mirrors the ``google_tts`` package layout: a thin API client, a
monthly seconds budget, and the pure alignment logic that maps
STT word offsets onto verse numbers using a reference text.
"""
