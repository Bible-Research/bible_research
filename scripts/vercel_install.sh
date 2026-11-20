#!/bin/sh
# Dependency install for the `bible-research-api` Vercel
# deployment (staging branch only — see
# `git.deploymentEnabled` in vercel.json).
#
# With `"framework": "django"` the preset creates its own
# virtualenv at `.vercel/python/.venv` BEFORE this command
# runs and puts it on PATH. Both the preset's
# DJANGO_SETTINGS probe (it imports manage.py to find the
# WSGI entrypoint) and the function bundle (the venv is
# vendored into the Lambda) read THAT venv. Installing
# anywhere else — e.g. the legacy
# `--target .vercel_python_packages` layout — leaves the
# venv empty and the build dies at the probe with
# `[DJANGO_SETTINGS_FAILED] ModuleNotFoundError: No module
# named 'django'`.
set -e
# Trace every command — the build log is the only observable
# output while iterating on the install environment.
set -x
# installCommand runs at the repo root in practice; pin cwd
# anyway so `.python-version` and `requirements.txt` resolve
# regardless.
cd "$(dirname "$0")/.."

# The venv interpreter: preset-created path first, PATH
# fallback (the preset puts .venv/bin on PATH for
# installCommand, so `python3` IS the venv python — observed
# in build logs). `uv pip install --python <venv>` installs
# into that interpreter's environment and resolves wheels
# for ITS version, so no .python-version cross-check is
# needed (the preset built the venv from that pin).
PYBIN=".vercel/python/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
[ -n "$PYBIN" ] || {
  echo "vercel_install: no venv python / python3 found" >&2
  exit 1
}
echo "vercel_install: python=$PYBIN"
"$PYBIN" -c 'import sys; print(sys.version)'

if command -v uv >/dev/null 2>&1 && \
   uv pip install --python "$PYBIN" -r requirements.txt; then
  exit 0
fi

# uv-created venvs ship no pip — bootstrap it via ensurepip
# for images where the uv binary is absent.
if "$PYBIN" -m ensurepip >/dev/null 2>&1 && \
   "$PYBIN" -m pip install -r requirements.txt; then
  exit 0
fi

# Last resort: standalone uv binary; `--python $PYBIN` still
# installs into the venv regardless of where uv came from.
echo "vercel_install: no uv/pip; bootstrapping uv"
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
"$UV_BIN" pip install --python "$PYBIN" -r requirements.txt
