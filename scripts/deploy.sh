#!/usr/bin/env bash
#
# Copy a checkout into the prefix the service actually runs from.
#
#   sudo ./scripts/deploy.sh [--prefix DIR] [--user NAME] [--pip]
#
# The service does not run from your checkout: systemd's unit sets
# WorkingDirectory=/opt/tpms and ProtectHome=true, so /home is not even visible
# to it. Installing and updating therefore both end in the same step -- putting
# the source where the service will look for it -- and that step lives here so
# the two cannot drift apart.
#
# Runtime state (config.yaml, the database, the raw archive) is left alone.
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/tpms}"
SERVICE_USER="${SERVICE_USER:-tpms}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP=0

# What a deploy consists of. Runtime state is deliberately absent.
ITEMS=(tpms tests systemd scripts pyproject.toml README.md config.example.yaml)

# Records the commit that was copied, so an updater can tell a stale prefix
# from a current one. Without it, "the checkout is up to date" gets mistaken
# for "the running service is up to date" -- which is how a deploy silently
# does nothing.
STAMP=".deployed"

usage() {
  cat <<USAGE
usage: $(basename "$0") [--prefix DIR] [--user NAME] [--pip]

  --prefix DIR  install prefix (default: /opt/tpms, or \$PREFIX)
  --user NAME   own the files as this user (default: tpms, or \$SERVICE_USER)
  --pip         reinstall the package into the venv as well
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift ;;
    --user)   SERVICE_USER="$2"; shift ;;
    --pip)    PIP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }

# Root is the normal case (/opt is not yours), but a prefix you already own
# needs no sudo -- and that is also what makes this script testable.
writable() {
  local target="$PREFIX"
  [ -e "$target" ] || target="$(dirname "$target")"
  [ -w "$target" ]
}
if [ "$(id -u)" -ne 0 ] && ! writable; then
  echo "run me with sudo -- $PREFIX is not yours to write" >&2
  exit 1
fi
[ -d "$SRC/tpms" ] || { echo "$SRC does not look like a tpms checkout" >&2; exit 1; }

if [ "$(readlink -f "$SRC")" = "$(readlink -f "$PREFIX")" ]; then
  say "Prefix is the checkout ($PREFIX) -- nothing to copy"
else
  say "Copying $SRC -> $PREFIX"
  mkdir -p "$PREFIX"
  for item in "${ITEMS[@]}"; do
    [ -e "$SRC/$item" ] || continue
    # Remove first. cp -r merges into an existing tree, so a module deleted
    # upstream would survive in the prefix and go on being importable -- the
    # kind of bug that only shows up as a stale page or a phantom import.
    rm -rf "${PREFIX:?}/$item"
    cp -r "$SRC/$item" "$PREFIX/"
  done
fi

if [ ! -f "$PREFIX/config.yaml" ]; then
  cp "$PREFIX/config.example.yaml" "$PREFIX/config.yaml"
  say "Wrote $PREFIX/config.yaml -- edit it to set frequency, gain and port"
fi

if [ ! -x "$PREFIX/.venv/bin/python" ]; then
  say "Building the virtualenv"
  python3 -m venv "$PREFIX/.venv"
  "$PREFIX/.venv/bin/pip" install -q --upgrade pip
  PIP=1
fi
if [ "$PIP" -eq 1 ]; then
  say "Installing the package"
  "$PREFIX/.venv/bin/pip" install -q -e "$PREFIX"
fi

if git -C "$SRC" -c safe.directory='*' rev-parse HEAD >"$PREFIX/$STAMP" 2>/dev/null; then
  :
else
  # Deployed from a tarball rather than a checkout: no commit to record, and a
  # stale stamp would be worse than none.
  rm -f "$PREFIX/$STAMP"
  warn "$SRC is not a git checkout; the updater cannot tell what is deployed"
fi

if [ "$(id -u)" -ne 0 ]; then
  :                                 # not root: the files are already ours
elif id -u "$SERVICE_USER" >/dev/null 2>&1; then
  chown -R "$SERVICE_USER":plugdev "$PREFIX"
else
  warn "user $SERVICE_USER does not exist; leaving ownership alone"
fi

deployed="$(cut -c1-7 "$PREFIX/$STAMP" 2>/dev/null || true)"
say "Deployed ${deployed:-an untracked tree} to $PREFIX"
