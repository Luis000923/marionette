#!/usr/bin/env bash
# One-time system setup for the control project. Run with: sudo ./scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo or pkexec: sudo ./scripts/install.sh" >&2
  exit 1
fi

if [[ -n "${SUDO_USER:-}" ]]; then
  REAL_USER="$SUDO_USER"
elif [[ -n "${PKEXEC_UID:-}" ]]; then
  REAL_USER="$(id -un "$PKEXEC_UID")"
else
  REAL_USER="$USER"
fi

echo "==> Installing ydotool, wl-clipboard and kdotool"
if command -v pacman >/dev/null 2>&1; then
  pacman -S --needed --noconfirm ydotool wl-clipboard
  if command -v kdotool >/dev/null 2>&1; then
    echo "kdotool already installed, skipping"
  elif pacman -Si kdotool >/dev/null 2>&1; then
    pacman -S --needed --noconfirm kdotool
  else
    echo "kdotool is not in the official Arch repos; install it from the AUR as $REAL_USER, e.g.:"
    echo "  yay -S kdotool"
  fi
elif command -v apt >/dev/null 2>&1; then
  apt update
  apt install -y ydotool wl-clipboard
  if command -v kdotool >/dev/null 2>&1; then
    echo "kdotool already installed, skipping"
  elif apt-cache show kdotool >/dev/null 2>&1; then
    apt install -y kdotool
  else
    echo "kdotool is not available via apt on this system; install it manually:"
    echo "  https://github.com/jinliu/kdotool"
  fi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y ydotool wl-clipboard
  if command -v kdotool >/dev/null 2>&1; then
    echo "kdotool already installed, skipping"
  elif dnf list kdotool >/dev/null 2>&1; then
    dnf install -y kdotool
  else
    echo "kdotool is not available via dnf on this system; install it manually:"
    echo "  https://github.com/jinliu/kdotool"
  fi
else
  echo "No supported package manager found (looked for pacman, apt, dnf)."
  echo "Please install ydotool and kdotool manually for your distro, then re-run this script"
  echo "(or just the systemctl/group steps below) once they're available."
fi

echo "==> Loading the uinput kernel module"
# The static /dev/uinput node exists from boot, but nothing had loaded the
# module, so udev never got a uevent to apply its permission rule to it.
modprobe uinput
echo uinput > /etc/modules-load.d/uinput.conf

echo "==> Granting $REAL_USER access to /dev/uinput"
# This ydotool build has no system-wide ydotoold.service; it ships a udev rule
# (GROUP=input, MODE=0660) plus a per-user systemd unit instead.
usermod -aG input "$REAL_USER"
udevadm control --reload-rules
udevadm trigger --name-match=uinput
sleep 0.5
ls -la /dev/uinput

echo "==> Enabling the ydotool user service for $REAL_USER"
REAL_UID="$(id -u "$REAL_USER")"
if [[ -S "/run/user/$REAL_UID/bus" ]]; then
  sudo -u "$REAL_USER" XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable --now ydotool.service
else
  echo "No active user session bus for $REAL_USER (/run/user/$REAL_UID/bus missing)."
  echo "Once logged in graphically, run this as $REAL_USER (not root):"
  echo "  systemctl --user enable --now ydotool.service"
fi

echo
echo "Done. Your user was just added to the 'input' group: log out and back in"
echo "(or run 'newgrp input') before ydotool commands will work without sudo."
echo
echo "If kdotool could not be installed automatically above, install it manually"
echo "before using any 'control win ...' commands."
echo
echo "Next: install the Playwright browser binaries as your normal user:"
echo "  cd $(dirname "$0")/.. && uv sync && uv run playwright install chromium"
