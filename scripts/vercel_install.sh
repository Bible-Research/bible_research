#!/bin/sh
# Dependency install for the `bible-research-api` Vercel
# deployment (staging branch only — see
# `git.deploymentEnabled` in vercel.json). Replaces the
# Django preset's own installer, which resolves wheels
# against the newest managed CPython (3.14+): compiled
# extensions built for a newer interpreter (psycopg2,
# grpcio, pillow, ...) cannot be imported by the runtime —
# observed as `No module named 'psycopg2._psycopg'` →
# ImproperlyConfigured at migrate time.
#
# Packages must land INSIDE the preset's build venv
# ($VERCEL_PYTHON_VENV_PATH → .vercel/python/.venv), not a
# `--target` dir: the preset's post-build hooks (Django
# settings read, collectstatic, bytecode compile) exec that
# venv's `python` with an env we cannot extend, and the
# function's `_vendor` bundle is assembled by scanning the
# venv's site-packages. Deps installed elsewhere are
# invisible at build time (DJANGO_SETTINGS_FAILED:
# `No module named 'django'`) and at runtime.
set -e
# Trace every command — staging-only build, and the log
# is the only observable output while iterating on the
# install environment.
set -x
# installCommand runs at the repo root in practice; pin
# cwd anyway so `.python-version`, `requirements.txt` and
# the fallback venv path resolve regardless.
cd "$(dirname "$0")/.."

PYVER="$(head -n1 .python-version | tr -d '[:space:]')"
PYMM="$(echo "$PYVER" | cut -d. -f1-2)"
VENV="${VERCEL_PYTHON_VENV_PATH:-$PWD/.vercel/python/.venv}"
VENV_PY="$VENV/bin/python"

# The pin no longer selects the install interpreter — the
# venv does — but keep requiring a numeric X.Y so a bad
# `.python-version` fails loudly instead of silently
# degrading the mismatch warning below.
case "$PYMM" in
  [0-9]*.[0-9]*) ;;
  *)
    echo "vercel_install: unsupported .python-version" \
      "'$PYVER' (need X.Y)" >&2
    exit 1
    ;;
esac

if [ ! -x "$VENV_PY" ]; then
  echo "vercel_install: no venv python at $VENV_PY" >&2
  exit 1
fi

# Wheels must match the venv interpreter — it runs the
# build hooks and the deployed function. A venv version
# differing from the pin means the preset ignored
# `.python-version`; installing for the venv is still
# correct (it is what runs), so warn but proceed.
VENV_MM="$("$VENV_PY" -c 'import sys
print("%d.%d" % sys.version_info[:2])')"
# pip `--target` fallbacks install into the venv's own
# site-packages so results match a real venv install.
SITE="$("$VENV_PY" -c \
  'import sysconfig; print(sysconfig.get_path("purelib"))')"

# Diagnostics — the build log must show which installer
# ran and which interpreters the image actually offers.
echo "vercel_install: pin=$PYVER venv=$VENV_PY" \
  "venv_mm=$VENV_MM site=$SITE"
[ "$VENV_MM" = "$PYMM" ] || \
  echo "vercel_install: WARNING venv $VENV_MM != pin $PYMM"
echo "vercel_install: uv=$(command -v uv || echo absent)"

if command -v uv >/dev/null 2>&1 && \
   uv pip install \
     --python "$VENV_PY" \
     -r requirements.txt; then
  exit 0
fi

# pip3 can belong to a different interpreter than the venv
# — Vercel's image carries a system Python for its own
# tooling. Wheels resolve for pip3's interpreter, so
# require it to match the venv and --target its
# site-packages.
PIP3V="$(pip3 --version 2>/dev/null | sed -n \
  's/.*(python \([0-9][0-9.]*\)).*/\1/p')"
PIP3MM="$(echo "$PIP3V" | cut -d. -f1-2)"
echo "vercel_install: pip3 python=${PIP3V:-absent}"
if [ -n "$PIP3V" ] && [ "$PIP3MM" = "$VENV_MM" ] && \
   pip3 install --target "$SITE" -r requirements.txt; then
  exit 0
fi

# uv is not on PATH for installCommand — the preset
# invokes its own binary by absolute path. Fall back to
# pip on an interpreter matching the venv: a mismatched
# one (e.g. a newer build) resolves unusable wheels.
# uv-managed CPython lives under its own dirs (the runtime
# traceback showed /uv/python/versions/...), not on PATH,
# so glob those before the PATH names.
for PY in \
  /uv/python/versions/*/bin/"python$VENV_MM" \
  "$HOME"/.local/share/uv/python/*/bin/"python$VENV_MM" \
  "python$VENV_MM" \
  python3
do
  command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || \
    continue
  if ! PYV="$("$PY" -c 'import sys
print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    continue
  fi
  [ "$PYV" = "$VENV_MM" ] || continue
  "$PY" -m pip --version >/dev/null 2>&1 || continue
  if "$PY" -m pip install \
       --target "$SITE" \
       -r requirements.txt; then
    exit 0
  fi
done

# uv can exist off PATH — the preset installs its own
# under ~/.local/bin or /usr/local/bin. Probe the known
# locations before paying for a download.
for UV_BIN in \
  "$HOME/.local/bin/uv" \
  "$HOME/.cargo/bin/uv" \
  /usr/local/bin/uv
do
  if [ -x "$UV_BIN" ] && "$UV_BIN" pip install \
       --python "$VENV_PY" \
       -r requirements.txt; then
    exit 0
  fi
done

# Last resort: bootstrap the standalone uv binary; it
# installs wheels resolved for the venv interpreter
# regardless of what is on PATH.
echo "vercel_install: no uv/venv-matched-pip;" \
  "bootstrapping uv"
if command -v curl >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
elif command -v wget >/dev/null 2>&1; then
  wget -qO- https://astral.sh/uv/install.sh | sh
else
  echo "vercel_install: no curl/wget for bootstrap" >&2
  exit 1
fi
UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || UV_BIN="$HOME/.cargo/bin/uv"
[ -x "$UV_BIN" ] || {
  echo "vercel_install: uv bootstrap failed" >&2
  exit 1
}
"$UV_BIN" pip install \
  --python "$VENV_PY" \
  -r requirements.txt
