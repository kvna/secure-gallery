# Secure Gallery

A self-hosted, encrypted local photo gallery. Each gallery lives in its own encrypted vault (gocryptfs), served by a lightweight Python web server with a clean browser UI.

## Features

- **Encrypted vaults** — each gallery encrypted at rest with gocryptfs; zip exports AES-256 encrypted
- **Multiple galleries** — run simultaneously on separate ports, each with its own vault and password
- **Auto-import** — drop photos into a watch folder; they appear in the gallery automatically, renamed by EXIF date
- **Duplicate detection** — SHA-256 exact + perceptual hash (pHash), scoped per gallery
- **Tagging & metadata** — keywords, categories, notes stored as sidecar JSON files
- **Gallery Hub** — browser UI to manage all galleries at `http://localhost:8764`

## Requirements

- Linux, WSL2 (Windows), or ChromeOS (Crostini)
- Python 3.8+
- gocryptfs (`sudo apt install gocryptfs`)

## Quick Start

```bash
git clone https://github.com/kvna/secure-gallery.git
cd secure-gallery
chmod +x firstrun.sh
./firstrun.sh
```

`firstrun.sh` detects your environment, installs dependencies, sets up a Python virtual environment, configures your drop folder, and walks you through creating your first gallery.

## Daily use

```bash
./start-gallery.sh <name>    # prompts for vault password, starts gallery
./start-hub.sh               # hub at http://localhost:8764
```

See **[ReallyREADME.md](ReallyREADME.md)** for the full usage cheat-sheet.

## How it works

```
secure-gallery/
  server.py        — Python HTTP server, all gallery logic
  gallery.html     — single-page frontend (no framework, no build step)
  hub.py           — gallery hub server
  hub.html         — hub UI
  monitor.py       — watches drop folder, moves files into active gallery
  start-gallery.sh — unlocks vault, starts monitor + server, locks on exit
  new-gallery.sh   — creates a new encrypted gallery
  start-hub.sh     — starts the hub
  firstrun.sh      — one-time setup
  galleries/       — created locally, not in repo (your data lives here)
```

Each gallery under `galleries/<name>/`:

| Path | Contents |
|---|---|
| `.vault/` | gocryptfs ciphertext — your encrypted data, always on disk |
| `vault/` | mountpoint — populated only while the server is running |
| `gallery.json` | name, port, description — plaintext metadata only |

When the server stops, the vault locks automatically and `vault/` goes empty.

## License

MIT
