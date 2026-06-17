# Bundled binaries

## `7zz` — 7-Zip console (x64)

Static 7-Zip CLI used to extract `.7z` ROM archives on SteamOS / Steam Deck,
which ships no system 7-Zip. `sync_core._find_7z()` resolves it at
`<plugin>/bin/7zz` (overridable via the `ROMM_7ZIP` env var). Optional — without
it, `.7z` console ROMs still load via RetroArch's native archive support; only
`.7z` PC games (which need extraction) are skipped.

- Version: 7-Zip 23.01 (2023-06-20), x86-64
- Source: https://www.7-zip.org/a/7z2301-linux-x64.tar.xz
- tarball sha256: `23babcab045b78016e443f862363e4ab63c77d75bc715c0b3463f6134cbcf318`
- `7zz` sha256: `c7f8769e2bc8df6bcbfba34571ee0340670a52dec824dbac844dd3b5bd1a69e1`
- License: the 7-Zip License (main code GNU LGPL v2.1+; some parts BSD 3-clause;
  the unRAR portion under its own restriction). See https://www.7-zip.org/license.txt

To update: download the current `7zXXXX-linux-x64.tar.xz`, extract `7zz`, replace
this file (keep it `chmod +x`), and update the hashes above.
