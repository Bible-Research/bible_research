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
# (where the preset expects deps on sys.path).
set -e
# installCommand runs at the repo root in practice; pin
# cwd anyway so `.python-version`, `requirements.txt` and
# the target dir resolve regardless.
cd "$(dirname "$0")/.."

PYVER="$(head -n1 .python-version | tr -d '[:space:]')"
PYMM="$(echo "$PYVER" | cut -d. -f1-2)"
TARGET=.vercel_python_packages

# Only a numeric X.Y pin is supported below — a name or
# major-only pin must fail loudly rather than resolve the
# newest interpreter and reproduce the drift bug this
# script exists to fix.
case "$PYMM" in
  [0-9]*.[0-9]*) ;;
  *)
    echo "vercel_install: unsupported .python-version" \
      "'$PYVER' (need X.Y)" >&2
    exit 1
    ;;
esac

# Diagnostics — the build log must show which installer
# ran and which interpreters the image actually offers.
echo "vercel_install: pin=$PYVER"
echo "vercel_install: uv=$(command -v uv || echo absent)"
for P in "python$PYMM" python3; do
  echo "vercel_install: $P=$(command -v "$P" || echo absent)"
done
MANAGED="$(ls -d \
  /uv/python/versions/*/bin/"python$PYMM" \
  "$HOME"/.local/share/uv/python/*/bin/"python$PYMM" \
  2>/dev/null | head -1)"
echo "vercel_install: managed=${MANAGED:-none}"

if command -v uv >/dev/null 2>&1 && \
   uv pip install \
     --python "$PYVER" \
     --target "$TARGET" \
     -r requirements.txt; then
  exit 0
fi

# uv is not on PATH for installCommand — the preset
# invokes its own binary by absolute path. Fall back to
# pip, but only on an interpreter matching the pin: a
# newer one (e.g. a 3.14 build venv) reproduces the
# original cp314-vs-3.12 wheel bug. uv-managed CPython
# lives under its own dirs (the runtime traceback showed
# /uv/python/versions/...), not on PATH, so glob those
# before the PATH names.
for PY in \
  /uv/python/versions/*/bin/"python$PYMM" \
  "$HOME"/.local/share/uv/python/*/bin/"python$PYMM" \
  "python$PYMM" \
  python3
do
  command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || \
    continue
  if ! PYV="$("$PY" -c 'import sys
print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    continue
  fi
  [ "$PYV" = "$PYMM" ] || continue
  "$PY" -m pip --version >/dev/null 2>&1 || continue
  if "$PY" -m pip install \
       --target "$TARGET" \
       -r requirements.txt; then
    exit 0
  fi
done

# Last resort: bootstrap the standalone uv binary; it
# resolves wheels for $PYVER via managed interpreters
# (the image carries cpython under uv's dirs; it can also
# download one) regardless of what is on PATH.
echo "vercel_install: no uv/pinned-pip; bootstrapping uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || UV_BIN="$HOME/.cargo/bin/uv"
[ -x "$UV_BIN" ] || {
  echo "vercel_install: uv bootstrap failed" >&2
  exit 1
}
"$UV_BIN" pip install \
  --python "$PYVER" \
  --target "$TARGET" \
  -r requirements.txt
