#!/usr/bin/env bash
#
# Install TPMS watch as a systemd service on a Raspberry Pi / Debian host.
# Idempotent: safe to re-run to upgrade an existing install.
#
#   sudo ./scripts/install-pi.sh
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/tpms}"
SERVICE_USER="${SERVICE_USER:-tpms}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "this script needs systemd" >&2; exit 1; }

# ---------------------------------------------------------------- packages
say "Installing packages"
apt-get update -qq
apt-get install -y --no-install-recommends rtl-433 rtl-sdr python3-venv python3-pip

# Debian bookworm ships rtl_433 22.11, which is years old. The app copes --
# it discovers which protocols your build supports instead of assuming -- but
# an old build decodes fewer TPMS sensors, so it is worth knowing about.
version="$(rtl_433 -V 2>&1 | head -1 || true)"
say "Installed: ${version}"
if printf '%s' "$version" | grep -qE '2[0-3]\.[0-9]+'; then
  warn "this rtl_433 predates many TPMS decoders."
  warn "for full coverage build from source: https://github.com/merbanan/rtl_433"
  warn "(CVE-2025-34450 also affects builds up to 25.02)"
fi

# ------------------------------------------------------------ DVB driver
# The kernel's DVB-T driver claims RTL-SDR dongles on sight, which makes
# rtl_433 fail with "usb_claim_interface error -6".
say "Blacklisting the DVB-T kernel driver"
cat > /etc/modprobe.d/blacklist-rtl-dvb.conf <<'BLACKLIST'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BLACKLIST
if lsmod | grep -q dvb_usb_rtl28xxu; then
  modprobe -r dvb_usb_rtl28xxu 2>/dev/null || warn "could not unload it now; reboot to finish"
fi

# ------------------------------------------------------------------- udev
say "Installing udev rules so a non-root user can open the dongle"
install -m 0644 "$SRC/systemd/99-rtl-sdr.rules" /etc/udev/rules.d/99-rtl-sdr.rules
if ! { udevadm control --reload-rules && udevadm trigger; }; then
  warn "udev reload failed; reboot to finish"
fi

# ------------------------------------------------------------------- user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating service user '$SERVICE_USER'"
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG plugdev "$SERVICE_USER"

# ------------------------------------------------------------------ files
say "Installing to $PREFIX"
mkdir -p "$PREFIX"
# Copy the source, leaving runtime state (db, config, raw archive) untouched.
for item in tpms tests systemd scripts pyproject.toml README.md config.example.yaml; do
  [ -e "$SRC/$item" ] && cp -r "$SRC/$item" "$PREFIX/"
done

if [ ! -f "$PREFIX/config.yaml" ]; then
  cp "$PREFIX/config.example.yaml" "$PREFIX/config.yaml"
  say "Wrote $PREFIX/config.yaml -- edit it to set frequency, gain and port"
else
  say "Keeping your existing $PREFIX/config.yaml"
fi

say "Building the virtualenv"
if [ ! -x "$PREFIX/.venv/bin/python" ]; then
  python3 -m venv "$PREFIX/.venv"
fi
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
"$PREFIX/.venv/bin/pip" install -q -e "$PREFIX"

chown -R "$SERVICE_USER":plugdev "$PREFIX"

# ---------------------------------------------------------------- service
say "Installing the systemd service"
install -m 0644 "$SRC/systemd/tpms.service" /etc/systemd/system/tpms.service
systemctl daemon-reload
systemctl enable tpms
systemctl restart tpms

sleep 3
say "Status"
systemctl --no-pager --lines=15 status tpms || true

port="$(grep -E '^\s*port:' "$PREFIX/config.yaml" | tail -1 | tr -dc '0-9' || echo 8080)"
cat <<DONE

Installed. The web UI should be at:

    http://$(hostname -I 2>/dev/null | awk '{print $1}'):${port:-8080}

Useful commands:
    journalctl -u tpms -f              follow the log
    sudo systemctl stop tpms           stop it (Restart=always, so use this)
    sudo -u $SERVICE_USER $PREFIX/.venv/bin/tpms --config $PREFIX/config.yaml status

If the receiver will not start, the Status page and the log now print
rtl_433's own error and a likely cause. A reboot is needed if the DVB
driver was loaded before this script ran.
DONE
