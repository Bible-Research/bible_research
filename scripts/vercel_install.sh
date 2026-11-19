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
# (where the preset expects deps) and `--python` must stay
# in sync with `.python-version`.
set -e

uv pip install \
  --python 3.12 \
  --target .vercel_python_packages \
  -r requirements.txt
