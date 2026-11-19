#!/bin/sh
# Build step for the `bible-research-api` Vercel deployment
# (staging branch only — see `git.deploymentEnabled` in
# vercel.json). Runs inside Vercel's Django preset build, so
# requirements are already installed and the `DOTENV` project
# env var supplies DATABASE_STAGING_URL/DB_SSL_CERT.
set -e

# Apply migrations, but never against a non-staging database.
# On Vercel previews settings.py pins DATABASE_STAGING_URL;
# the var may live inside the DOTENV payload rather than as a
# real env var, so resolve it through the repo's own .env
# parser first (same setdefault precedence: real env wins).
if [ -z "$DATABASE_STAGING_URL" ] && [ -n "$DOTENV" ]; then
  DATABASE_STAGING_URL=$(python3 -c '
import os
from bible_research.dotenv import parse_dotenv
print(parse_dotenv(os.environ["DOTENV"]).get("DATABASE_STAGING_URL", ""))
')
fi
# Export so Django consumes exactly the validated value —
# an empty real env var would otherwise shadow a valid
# DOTENV-carried URL (load_dotenv uses setdefault).
export DATABASE_STAGING_URL
if ! python3 -c '
import os, sys
import dj_database_url
try:
    name = dj_database_url.parse(
        os.environ["DATABASE_STAGING_URL"]
    )["NAME"]
except Exception:
    name = ""
sys.exit(0 if name == "stagingdb" else 1)
'; then
  echo "Refusing to migrate: DATABASE_STAGING_URL must name" \
    "stagingdb on a preview deployment." >&2
  exit 1
fi
python3 manage.py migrate --noinput

# The preset runs collectstatic automatically; running it here
# too keeps static output populated even if a custom
# buildCommand replaces the preset's own step. Idempotent.
python3 manage.py collectstatic --noinput
