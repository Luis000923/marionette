# Third-party software

## Obscura

- Project: https://github.com/h4ckf0r0day/obscura
- Author: **h4ckf0r0day** and contributors
- License: Apache-2.0 (https://github.com/h4ckf0r0day/obscura/blob/main/LICENSE)

Marionette uses Obscura, an open-source headless browser engine written in Rust, as
the default reader for simple browsing tasks (`control browser fetch`). Obscura is
not vendored in this repository: `scripts/install_obscura.sh` clones and builds it
into the git-ignored `obscura/` folder, and `CONTROL_OBSCURA` can point at any build.
All credit for the engine goes to its author.
