#!/usr/bin/env bash
# setup.sh — one-shot setup for recur-recur v2 on Raspberry Pi OS Bookworm (64-bit)
#
# v2 architecture: mpv is the only renderer. Shaders run as mpv GLSL hooks.
# No glslViewer, no shaderbang — those don't work cleanly on Pi 5's V3D driver.

set -euo pipefail

echo "==> recur-recur v2 setup for Raspberry Pi 5"

# --- system packages ---------------------------------------------------------
echo "==> installing system packages…"
sudo apt update
sudo apt install -y \
    mpv ffmpeg \
    python3-picamera2 python3-lgpio python3-spidev python3-pip \
    python3-pil python3-numpy python3-evdev \
    libasound2-dev libjack-jackd2-dev \
    pmount \
    git build-essential

# --- python packages (the ones not available via apt) ------------------------
echo "==> installing python packages…"
pip install python-rtmidi gpiozero --break-system-packages

# --- groups: input (keyboard/MIDI), video (DRM/KMS), render (V3D) -----------
echo "==> adding $USER to input + video + render groups…"
sudo usermod -aG input,video,render "$USER"

# --- boot to console, no desktop ---------------------------------------------
# Pi 5 ships with labwc autostarting on tty1; that holds DRM master and
# blocks mpv from grabbing the screen. We boot to text console instead.
echo "==> setting boot mode to console autologin (no desktop)…"
sudo raspi-config nonint do_boot_behaviour B2

# --- boot config: SPI + GPU --------------------------------------------------
CONFIG=/boot/firmware/config.txt
echo "==> tuning $CONFIG…"

# SPI0 must be enabled for the 3.5" SPI display (ILI9486 on /dev/spidev0.0).
# The default Pi OS config leaves it commented out.
if grep -q "^#dtparam=spi=on" "$CONFIG"; then
    sudo sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' "$CONFIG"
    echo "    enabled dtparam=spi=on"
elif ! grep -q "^dtparam=spi=on" "$CONFIG"; then
    echo "dtparam=spi=on" | sudo tee -a "$CONFIG"
    echo "    added dtparam=spi=on"
else
    echo "    dtparam=spi=on already set"
fi

if ! grep -q "v3d_freq_min" "$CONFIG"; then
    echo "# recur-recur: prevent GPU frequency downscaling" | sudo tee -a "$CONFIG"
    echo "v3d_freq_min=500" | sudo tee -a "$CONFIG"
fi

cat <<'POST'

==> setup complete.

NEXT STEPS:
  1. reboot:           sudo reboot
  2. land on console:  Pi will auto-log you in to a text prompt
  3. put clips in:     ~/recur-recur/clips/
  4. run:              cd ~/recur-recur && python3 main.py

CONTROLS (keyboard):
  TAB       cycle mode (SAMPLER / SHADER / LIVE / FX)
  0-9       trigger clip slot
  Space     trigger from in-point (hold for gated)
  arrows    prev/next clip, speed up/down
  M         cycle sampler playback mode
  I O C     set in / out / clear points
  R         reverse
  [ ]       prev / next shader
  `         toggle recording
  Q         quit

OPTIONAL: for boot-as-appliance, run ./install-service.sh after this works.

POST
echo "==> reboot recommended:  sudo reboot"
