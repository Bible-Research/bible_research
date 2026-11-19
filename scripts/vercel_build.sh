#!/bin/sh
# Build step for the `bible-research-api` Vercel deployment
# (staging branch only — see `git.deploymentEnabled` in
# vercel.json). Runs inside Vercel's Django preset build, so
# requirements are already installed and the `DOTENV` project
# env var supplies DATABASE_URL/DB_SSL_CERT.
set -e

# Apply migrations, but never against a non-staging database —
# a misconfigured DOTENV carrying the prod env-file's
# `defaultdb` DATABASE_URL must fail the build loudly instead
# of migrating production.
case "$DATABASE_URL" in
  *stagingdb*)
    python3 manage.py migrate --noinput
    ;;
  *)
    echo "Refusing to migrate: DATABASE_URL is not stagingdb." >&2
    exit 1
    ;;
esac

# The preset runs collectstatic automatically; running it here
# too keeps static output populated even if a custom
# buildCommand replaces the preset's own step. Idempotent.
python3 manage.py collectstatic --noinput
