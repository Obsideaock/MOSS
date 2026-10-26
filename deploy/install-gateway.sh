#!/usr/bin/env bash
# Set up the MOSS Seed Vault (the gateway) on a Raspberry Pi (64-bit Raspberry Pi OS).
#
#   sudo bash deploy/install-gateway.sh      (from anywhere; sh works too)
#
# Safe to re-run. It creates .venv, installs the gateway requirements,
# lets your user open the radio's serial port, and installs a systemd
# service that starts the gateway on boot. It never overwrites an edited
# /etc/moss/gateway.env.
# Started with `sh` instead of bash? Hand over to bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "run with sudo: sudo bash deploy/install-gateway.sh" >&2
  exit 1
fi
USER_NAME="${SUDO_USER:?run with sudo from your normal user}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "== packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

echo "== python environment in $REPO/.venv"
sudo -u "$USER_NAME" python3 -m venv "$REPO/.venv"
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements-gateway.txt"

echo "== serial port access for $USER_NAME"
usermod -aG dialout "$USER_NAME"

echo "== settings /etc/moss/gateway.env"
mkdir -p /etc/moss
if [[ ! -f /etc/moss/gateway.env ]]; then
  cp "$REPO/deploy/gateway.env" /etc/moss/gateway.env
  echo "   created; edit MOSS_PORT before starting"
else
  echo "   kept your existing file"
fi

echo "== systemd service moss-gateway"
sed -e "s|@USER@|$USER_NAME|g" -e "s|@REPO@|$REPO|g" \
    "$REPO/deploy/moss-gateway.service" > /etc/systemd/system/moss-gateway.service
systemctl daemon-reload
systemctl enable moss-gateway >/dev/null

cat <<MSG

Done. Next:
  1. Plug in the KIWIX USB stick; check MOSS_ZIM in the settings matches where
     the .zim shows up (default /media/moss/KIWIX/...). For it to mount at boot
     with nobody logged in, see "USB stick" in README.md.
  2. Find the radio:   ls -l /dev/serial/by-id/
  3. Edit settings:    sudo nano /etc/moss/gateway.env
  4. Try it by hand:   $REPO/.venv/bin/python -m moss gateway --source /media/moss/KIWIX/<file>.zim --port <port>
  5. Then run it as a service:
       sudo systemctl start moss-gateway
       journalctl -u moss-gateway -f        # watch requests come in
MSG
