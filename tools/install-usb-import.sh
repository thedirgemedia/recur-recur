#!/usr/bin/env bash
# install-usb-import.sh — enable on-demand USB import for recur.
#
# Installs the read-only mount helper and a passwordless sudoers rule so the
# recur service can mount removable drives when you ask it to (in the IMPORT
# menu page). Run with sudo from the project directory:  sudo ./tools/install-usb-import.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "==> installing recur-usb helper to /usr/local/sbin"
install -m 0755 "$HERE/recur-usb" /usr/local/sbin/recur-usb

echo "==> installing sudoers rule for user '$USER_NAME'"
sed "s/^dirge /$USER_NAME /" "$HERE/recur-usb.sudoers" > /etc/sudoers.d/recur-usb
chmod 0440 /etc/sudoers.d/recur-usb
visudo -cf /etc/sudoers.d/recur-usb >/dev/null && echo "    sudoers rule OK"

echo "==> done. Restart recur:  sudo systemctl restart recur"
echo "    Then plug a USB drive and open the IMPORT page in the menu."
