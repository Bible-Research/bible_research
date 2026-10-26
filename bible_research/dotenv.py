"""Minimal .env parsing without external dependencies.

Supports ``KEY=VALUE``, single/double-quoted values, ``#`` comments
(including ``value # comment`` for unquoted values) and blank lines.
Inside double-quoted values a literal ``\\n`` is decoded to a real
newline — this is how multi-line values such as the ``DB_SSL_CERT``
PEM block are stored on one line.
"""

import os


def parse_dotenv(contents):
    """Parse .env text into a dict.

    Args:
        contents: The full text of a .env file.

    Returns:
        A dict mapping variable names to their values.
    """
    values = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\n', '\n')
        elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            value = value[1:-1]
        elif ' #' in value:
            value = value.split(' #', 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_dotenv(contents):
    """Apply .env text to os.environ.

    Existing environment variables win (``setdefault`` semantics), so
    real env vars always override .env-provided values.
    """
    for key, value in parse_dotenv(contents).items():
        os.environ.setdefault(key, value)


def load_dotenv_file(path):
    """Read the file at ``path`` and apply it via load_dotenv."""
    with open(path, 'r') as f:
        load_dotenv(f.read())
