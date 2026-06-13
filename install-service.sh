#!/usr/bin/env bash
# install-service.sh — make recur-recur launch on boot as an appliance.
#
# Does two things:
#   1. Sets the Pi to boot to console (no desktop) with autologin.
#   2. Installs + enables a systemd service that runs the instrument on tty1.
#
# Run from inside the recur-recur directory:  ./install-service.sh
set -euo pipefail

USER_NAME="${SUDO_USER:-$USER}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> installing recur-recur as a boot service"
echo "    user:    $USER_NAME"
echo "    project: $PROJECT_DIR"

# --- 1. boot to console (no desktop) ----------------------------------------
# B2 = console autologin in raspi-config's nonint API.
echo "==> setting boot target to console + autologin…"
sudo raspi-config nonint do_boot_behaviour B2

# --- 2. build the service file with the right paths -------------------------
echo "==> writing /etc/systemd/system/recur.service…"
sudo tee /etc/systemd/system/recur.service >/dev/null <<EOF
[Unit]
Description=recur-recur video instrument
After=multi-user.target systemd-udev-settle.service
Wants=systemd-udev-settle.service
# Don't fight the getty on tty1
Conflicts=getty@tty1.service
After=getty@tty1.service

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 $PROJECT_DIR/main.py --output hdmi --mode SHADER

StandardInput=tty
StandardOutput=journal
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

SupplementaryGroups=video render input audio spi gpio

# CAP_SYS_NICE: scheduler priority for mpv.
# CAP_SYS_ADMIN: needed by ffmpeg kmsgrab (recording) and by the USB-import
#   mount helper — the bounding set is the ceiling for child processes, so
#   without it even a sudo-escalated 'mount' fails with EPERM.
AmbientCapabilities=CAP_SYS_NICE CAP_SYS_ADMIN
CapabilityBoundingSet=CAP_SYS_NICE CAP_SYS_ADMIN

Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# --- 3. enable -------------------------------------------------------------
echo "==> enabling service…"
sudo systemctl daemon-reload
sudo systemctl enable recur.service

cat <<EOF

==> done.

The instrument will launch automatically on the next boot, straight to tty1.

Useful commands:
  sudo systemctl start   recur     # start now without rebooting
  sudo systemctl stop    recur     # stop it
  sudo systemctl restart recur     # restart (e.g. after editing main.py)
  sudo systemctl disable recur     # stop launching on boot
  journalctl -u recur -f           # watch its logs live

To change the start mode/output, edit ExecStart in
  /etc/systemd/system/recur.service
then:  sudo systemctl daemon-reload && sudo systemctl restart recur

Reboot now to try it:  sudo reboot
EOF
