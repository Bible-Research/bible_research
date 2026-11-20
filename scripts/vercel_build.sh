#!/bin/sh
# Build step for the `bible-research-api` Vercel deployment
# (staging branch only — see `git.deploymentEnabled` in
# vercel.json). Runs inside Vercel's Django preset build, so
# requirements are already installed and the `DOTENV` project
# env var supplies DATABASE_STAGING_URL/DB_SSL_CERT.
set -e
# PYBIN below is relative — pin cwd to the repo root so it
# resolves regardless of where buildCommand is invoked.
cd "$(dirname "$0")/.."

# vercel_install.sh installs requirements into the preset's
# own venv (.vercel/python/.venv) — use that interpreter
# explicitly rather than relying on it staying on PATH.
PYBIN=".vercel/python/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then
  echo "vercel_build: preset venv missing — deps may" \
    "land outside the vendored venv" >&2
  PYBIN="$(command -v python3 || true)"
fi
[ -n "$PYBIN" ] || {
  echo "vercel_build: no venv python / python3 found" >&2
  exit 1
}

# Apply migrations, but never against a non-staging database.
# On Vercel previews settings.py pins DATABASE_STAGING_URL;
# the var may live inside the DOTENV payload rather than as a
# real env var, so resolve it through the repo's own .env
# parser first (same setdefault precedence: real env wins).
if [ -z "$DATABASE_STAGING_URL" ] && [ -n "$DOTENV" ]; then
  DATABASE_STAGING_URL=$("$PYBIN" -c '
import os
from bible_research.dotenv import parse_dotenv
print(parse_dotenv(os.environ["DOTENV"]).get("DATABASE_STAGING_URL", ""))
')
fi
# Export so Django consumes exactly the validated value —
# an empty real env var would otherwise shadow a valid
# DOTENV-carried URL (load_dotenv uses setdefault).
export DATABASE_STAGING_URL
# Stdlib parsing keeps the guard independent of where the
# install landed. Print the resolved DB name (not a
# credential) so a refusal log distinguishes "var unset" from
# "wrong database".
DB_NAME=$("$PYBIN" -c '
import os
from urllib.parse import urlparse
try:
    print(urlparse(
        os.environ["DATABASE_STAGING_URL"]
    ).path.lstrip("/"))
except Exception:
    print("")
')
if [ "$DB_NAME" != "stagingdb" ]; then
  echo "Refusing to migrate: DATABASE_STAGING_URL must name" \
    "stagingdb on a preview deployment" \
    "(resolved name: '${DB_NAME:-<unset>}')." >&2
  exit 1
fi
"$PYBIN" manage.py migrate --noinput

# The preset runs collectstatic automatically; running it here
# too keeps static output populated even if a custom
# buildCommand replaces the preset's own step. Idempotent.
"$PYBIN" manage.py collectstatic --noinput
