#!/usr/bin/env bash
#
# Update an installed TPMS watch: stop the service, pull, reinstall if the
# dependencies moved, start it again, and check it actually came back.
#
#   ./scripts/update-pi.sh
#
# Run it as the user who owns the checkout, not as root -- git writing files
# as root into a user checkout is a mess to undo. It calls sudo itself for the
# two systemctl steps that need it.
#
# If the service does not come back, the update is rolled back to the commit
# that was running before it, so a bad pull does not leave the Pi deaf.
#
set -euo pipefail

SERVICE="${SERVICE:-tpms}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLLBACK=1
STASH=0
WAIT=15

usage() {
  cat <<USAGE
usage: $(basename "$0") [--stash] [--no-rollback] [--service NAME]

  --stash         stash local changes instead of refusing to run
  --no-rollback   leave the new commit in place even if the service fails
  --service NAME  systemd unit to restart (default: tpms, or \$SERVICE)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stash)       STASH=1 ;;
    --no-rollback) ROLLBACK=0 ;;
    --service)     SERVICE="$2"; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
fail() { printf '\033[31m    %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO"
[ -d .git ] || fail "$REPO is not a git checkout; use scripts/install-pi.sh instead"
command -v systemctl >/dev/null || fail "this script needs systemd"

# Root would work but leaves root-owned objects in .git and __pycache__, and
# the next pull as the normal user then fails on permissions.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  fail "run me as $SUDO_USER, without sudo -- I will ask for it when I need it"
fi

# --------------------------------------------------------------- pre-flight
if [ -n "$(git status --porcelain)" ]; then
  if [ "$STASH" -eq 1 ]; then
    say "Stashing local changes"
    git stash push -u -m "update-pi $(date +%Y-%m-%dT%H:%M:%S)"
    warn "restore them later with: git stash pop"
  else
    git status --short
    fail "local changes would be overwritten; commit them, or re-run with --stash"
  fi
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BEFORE="$(git rev-parse HEAD)"
say "Checking for updates on $BRANCH"
git fetch --quiet origin "$BRANCH"
AFTER="$(git rev-parse "origin/$BRANCH")"

# --is-ancestor, not string equality: a checkout carrying a local commit is
# ahead of origin, not behind it, and has nothing to pull.
if git merge-base --is-ancestor "$AFTER" HEAD; then
  say "Already up to date ($(git log -1 --format='%h %s'))"
  # An `&& exit 0` here would take set -e with it when the service is down.
  if systemctl is-active --quiet "$SERVICE"; then
    exit 0
  fi
  warn "but $SERVICE is not running -- starting it"
fi

if ! git merge-base --is-ancestor HEAD "$AFTER"; then
  fail "$BRANCH has diverged from origin/$BRANCH; sort that out by hand first"
fi

# ------------------------------------------------------------------- update
# Whatever goes wrong from here, the Pi must not be left deaf. This covers the
# paths the explicit rollback below does not: a failed pull, a failed pip.
STOPPED=0
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$STOPPED" -eq 1 ] && ! systemctl is-active --quiet "$SERVICE"; then
    warn "update failed -- restarting $SERVICE on whatever is checked out now"
    sudo systemctl start "$SERVICE" || true
  fi
}
trap on_exit EXIT

say "Stopping $SERVICE"
sudo systemctl stop "$SERVICE"
STOPPED=1

say "Pulling"
# --ff-only: a merge commit created unattended on a Pi is nobody's friend.
git merge --ff-only "origin/$BRANCH"
git log --oneline "$BEFORE..HEAD" | sed 's/^/    /'

start() {
  say "Starting $SERVICE"
  sudo systemctl start "$SERVICE"
  # systemd reports "active" the moment the process forks, so give the radio
  # and the web server a moment before believing it.
  for _ in $(seq "$WAIT"); do
    sleep 1
    systemctl is-active --quiet "$SERVICE" || return 1
  done
}

# The venv only needs rebuilding when the packaging moved; pip install -e on
# every update costs a minute on a Pi for nothing.
if [ ! -x .venv/bin/python ]; then
  say "Creating the virtualenv"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
elif ! git diff --quiet "$BEFORE" HEAD -- pyproject.toml; then
  say "pyproject.toml changed -- reinstalling dependencies"
  .venv/bin/pip install -q -e .
fi

if start; then
  say "Running $(git log -1 --format='%h %s')"
  systemctl --no-pager --lines=8 status "$SERVICE" || true

  port="$(grep -E '^\s*port:' config.yaml 2>/dev/null | tail -1 | tr -dc '0-9' || true)"
  port="${port:-8080}"
  if command -v curl >/dev/null && curl -fsS -m 5 "http://127.0.0.1:$port/api/status" >/dev/null; then
    printf '\n    UI answering on http://%s:%s\n' \
      "$(hostname -I 2>/dev/null | awk '{print $1}')" "$port"
  else
    warn "the service is up but the web UI did not answer on port $port"
    warn "check: sudo journalctl -u $SERVICE -n 50"
  fi
  exit 0
fi

# ----------------------------------------------------------------- rollback
warn "$SERVICE did not stay running after the update"
# sudo: reading a system unit's journal is not open to every user.
sudo journalctl -u "$SERVICE" -n 20 --no-pager || true

if [ "$ROLLBACK" -eq 0 ]; then
  fail "left at $(git rev-parse --short HEAD); roll back with: git reset --hard $BEFORE"
fi

say "Rolling back to $(git log -1 --format='%h %s' "$BEFORE")"
git reset --hard --quiet "$BEFORE"
if ! git diff --quiet "$AFTER" HEAD -- pyproject.toml; then
  .venv/bin/pip install -q -e .
fi
if start; then
  fail "rolled back and running again; the new commit is the problem, not the Pi"
fi
fail "still not running after the rollback -- see the log above"
