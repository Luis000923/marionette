#!/usr/bin/env bash
# One-time system setup for the control project. Run with: sudo ./scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo ./scripts/install.sh" >&2
  exit 1
fi

REAL_USER="${SUDO_USER:-$USER}"

echo "==> Installing ydotool and kdotool"
if command -v pacman >/dev/null 2>&1; then
  pacman -S --needed --noconfirm ydotool
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
  apt install -y ydotool
  if command -v kdotool >/dev/null 2>&1; then
    echo "kdotool already installed, skipping"
  elif apt-cache show kdotool >/dev/null 2>&1; then
    apt install -y kdotool
  else
    echo "kdotool is not available via apt on this system; install it manually:"
    echo "  https://github.com/jinliu/kdotool"
  fi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y ydotool
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

echo "==> Enabling ydotoold system service"
if command -v ydotoold >/dev/null 2>&1 || systemctl list-unit-files 2>/dev/null | grep -q ydotoold; then
  systemctl enable --now ydotoold.service
else
  echo "ydotoold not found; skipping service enable. Install ydotool first, then run:"
  echo "  sudo systemctl enable --now ydotoold.service"
fi

echo "==> Granting $REAL_USER access to the ydotool socket"
getent group ydotoold >/dev/null 2>&1 && usermod -aG ydotoold "$REAL_USER" || true

echo
echo "Done. If your user was just added to a new group, log out and back in"
echo "(or run 'newgrp ydotoold') before ydotool commands will work without sudo."
echo
echo "If kdotool could not be installed automatically above, install it manually"
echo "before using any 'control win ...' commands."
echo
echo "Next: install the Playwright browser binaries as your normal user:"
echo "  cd $(dirname "$0")/.. && uv sync && uv run playwright install chromium"
