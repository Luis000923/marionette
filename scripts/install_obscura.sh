#!/usr/bin/env bash
# Fetch and build Obscura (https://github.com/h4ckf0r0day/obscura, Apache-2.0),
# the headless engine `control browser fetch` uses by default.
# Result: obscura/target/release/obscura (found automatically), or set CONTROL_OBSCURA.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d obscura/.git ] || git clone --depth 1 https://github.com/h4ckf0r0day/obscura.git obscura
if [ ! -x obscura/target/release/obscura ]; then
  command -v cargo >/dev/null || { echo "cargo not found: install Rust (rustup) first" >&2; exit 1; }
  (cd obscura && cargo build --release)
fi
obscura/target/release/obscura --version
