#!/bin/sh
# Dependency install for the `bible-research-api` Vercel
# deployment (staging branch only — see
# `git.deploymentEnabled` in vercel.json). Replaces the
# Django preset's own installer, which ignores
# `.python-version` and resolves wheels against the newest
# managed CPython (3.14+): compiled extensions built for
# cp314 (psycopg2, grpcio, pillow, ...) cannot be imported
# by the CPython 3.12 runtime the build command and the
# function actually run — observed as `No module named
# 'psycopg2._psycopg'` → ImproperlyConfigured at migrate
# time. `--target` must stay `.vercel_python_packages`
# (where the preset expects deps) and `--python` is read
# from `.python-version` so the pin cannot drift.
set -e

if command -v uv >/dev/null 2>&1; then
  uv pip install \
    --python "$(cat .python-version)" \
    --target .vercel_python_packages \
    -r requirements.txt
else
  # uv absent from the installCommand PATH — fall back to
  # the build/runtime interpreter's own pip (python3 is
  # CPython 3.12 in this image). If pip is missing too the
  # build fails loudly here, not later at migrate.
  python3 -m pip install \
    --target .vercel_python_packages \
    -r requirements.txt
fi
