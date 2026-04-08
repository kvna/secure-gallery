# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Multi-gallery structure

Each gallery lives under `~/ns/galleries/`. Galleries with a vault (default) use gocryptfs encryption:

```
~/ns/galleries/
  <name>/
    gallery.json        ← plaintext metadata (name, description, port, vault flag)
    .vault/             ← gocryptfs ciphertext directory (always on disk)
    vault/              ← gocryptfs mountpoint — empty when locked, contains everything when unlocked
      photos/           ← images; server --folder points here
      .gallery_session/ ← zip session extraction dir (also inside encrypted volume)
      .thumbcache/      ← auto-created
      duplicates/       ← auto-created; scoped to this gallery
      .hash_index.json
```

For non-vault galleries (`--no-vault`): structure is flat with `photos/` directly under `<name>/`.

Duplicate detection, thumbnail cache, and tag sidecars are all scoped to the individual gallery.

## Scripts

| Script | Purpose |
|---|---|
| `~/ns/new-gallery.sh <name> [desc]` | Scaffold a new gallery directory + `gallery.json` |
| `~/ns/start-gallery.sh <name> [args]` | Start a gallery server; reads port from `gallery.json` |
| `~/ns/start-hub.sh` | Start the Gallery Hub on port 8764 |

### `gallery.json` format

```json
{
  "name": "noir",
  "description": "Black and white photography",
  "created": "2026-04-08T12:00:00Z",
  "port": 8765
}
```

`port` determines which port `start-gallery.sh` uses by default (can be overridden with `--port N`). All galleries default to 8765 (one running at a time); assign distinct ports to run multiple simultaneously.

### Running galleries

```bash
~/ns/new-gallery.sh noir "Black and white"   # create gallery with encrypted vault (prompts for password)
~/ns/new-gallery.sh noir --no-vault          # create without encryption
~/ns/start-gallery.sh noir                   # start — prompts for vault password, mounts, then serves
~/ns/start-gallery.sh noir --minimal         # debug mode
~/ns/start-gallery.sh noir --port 8766       # port override
```

**Never invoke `python3 server.py` directly** — the launcher handles vault mounting, venv activation, and constructs the right `--folder` path.

### Vault / encryption

Two-layer encryption:

1. **gocryptfs** (at rest): `.vault/` holds ciphertext; `vault/` is the FUSE mountpoint. On server stop, the launcher unmounts automatically — `vault/` goes empty, data is inaccessible without the password.

2. **pyzipper AES-256** (zip import/export): when `GALLERY_PASSWORD` is set in the environment (done by the launcher), `zip_session()` and `extract_zip_to_session()` use AES-256 instead of plain zip. Falls back to unencrypted zip if pyzipper is absent or no password is set. The `HAS_PYZIPPER` and `GALLERY_PASSWORD` module-level vars in `server.py` control this.

## Gallery Hub

`hub.py` is a lightweight server (port 8764) that lists all galleries in `~/ns/galleries/`, shows running status, image counts, and provides Start/Stop controls.

```bash
~/ns/start-hub.sh   # start the hub; open http://localhost:8764
```

Hub API: `GET /api/galleries` → gallery list with status; `POST /api/start {slug}` → launch gallery; `POST /api/stop {port}` → kill gallery process.

## Dependencies & Setup

Requires a Python virtual environment:

```bash
python3 -m venv ~/gallery-env
source ~/gallery-env/bin/activate
pip install Pillow piexif imagehash watchdog pyzipper
```

For vault encryption, also install gocryptfs:
```bash
sudo apt install gocryptfs
```

`HAS_PIL` and `HAS_IMAGEHASH` flags in `server.py` gate optional features — thumbnails and EXIF require Pillow; duplicate detection requires imagehash.

## Architecture

This is a two-file application:

- **`server.py`** — Python HTTP server (stdlib `ThreadingHTTPServer`). All logic lives here: image serving, thumbnail generation, session/zip management, duplicate detection, tagging, file watching.
- **`gallery.html`** — Self-contained single-page frontend. Fetches everything via the REST API. No build step.

### server.py structure

| Section | What it does |
|---|---|
| Top-level constants & helpers | `SUPPORTED_EXTENSIONS`, `SESSION_FOLDER`, EXIF label maps, format helpers |
| `extract_metadata()` | Reads EXIF/GPS/file info for the metadata panel |
| `make_thumbnail()` | Generates resized JPEG, caches to `.thumbcache/` inside the photos folder |
| Duplicate detection | SHA-256 exact match + pHash (threshold 8) via `imagehash`; moves dupes to `../duplicates/` |
| Session/zip management | `SESSION` object handles zip extract, auto-rename on ingest, zip-on-logout |
| `_fast_walk()` | `os.scandir`-based recursive walk, skips excluded dirs (`.thumbcache`, `duplicates/`, `previous_sessions/`, etc.) |
| `GalleryHandler` | `do_GET` / `do_POST` route all `/api/*` endpoints |
| Watchdog watcher | Monitors `photos/` for new files, moves & renames them into the session folder, triggers hash/dup checks |

### REST API surface

**GET endpoints:**
- `/api/images` — full image list with hash for polling
- `/api/images/watch` — lightweight poll (hash + count only)
- `/api/thumbnail?file=<rel>` — base64 JPEG thumbnail
- `/api/image?file=<rel>` — raw image file
- `/api/metadata?file=<rel>` — EXIF/file metadata
- `/api/tags?file=<rel>` — sidecar tag data
- `/api/tags/all` — all tags across all images
- `/api/duplicates` — duplicate log + files in `duplicates/`
- `/api/session` — current session info
- `/api/debug/scan` — full scan report for debugging

**POST endpoints (JSON body):**
- `/api/tags` — save tags for a file
- `/api/tags/export` — write tags back into EXIF
- `/api/rename` — rename a file
- `/api/logout` — zip session and exit
- `/api/duplicates/restore` — move file from `duplicates/` back
- `/api/duplicates/delete` — permanently delete from `duplicates/`

### Tag sidecars

Tags are stored as `<imagename>.gallery.json` files alongside images with keys `keywords`, `category`, `notes`. These are included in the zip on logout.

### Session / zip flow

On startup with a zip: extract to `.gallery_session/` (sibling of `photos/`). On logout (browser ⏏ button or Ctrl+C → Y): zip everything (images + sidecars) back to `photos-YYYY-MM-DD-HHMMSS.zip`, move previous zip to `previous_sessions/`.

### Auto-rename

New images ingested via the file watcher or at session load are renamed to `image-YYYY-MM-DD-HHMMSS.ext` using EXIF `DateTimeOriginal` → file mtime as fallback.

### Key global state in server.py

The server uses module-level globals for shared state across threads. These are the most important ones to know when reading or modifying the backend:

| Variable | Purpose |
|---|---|
| `SESSION` | `Session` object — holds current session paths (`photos_dir`, `session_dir`, `source_zip`) |
| `_image_list` | Cached list of image filenames, protected by `_image_list_lock` |
| `_current_hash` | MD5 folder hash for lightweight browser polling |
| `_startup_complete` | `bool` — gates "not ready yet" responses during initial indexing |
| `_new_files` | `set` of filenames added this session (badged in the UI) |
| `_dup_log` | List of duplicate-move history entries |
| `MINIMAL_MODE` | Disables watcher, auto-rename, and duplicate detection entirely |

Route dispatch is a flat `if/elif` chain inside `do_GET` and `do_POST` in `GalleryHandler`. There is no routing library.

### monitor.py

Standalone file-mover: watches a source folder and moves files into a gallery's `photos/` dir. Operates at 60s intervals normally, 5s (fast mode) when files are found.

```bash
python3 monitor.py                        # default source → ~/ns/photos
python3 monitor.py --gallery noir         # → ~/ns/galleries/noir/photos
python3 monitor.py --source /p --dest /q  # explicit paths
```

### gallery.html architecture

~3,900 lines of vanilla JavaScript — no framework, no build step, no external JS libraries. CSS custom properties define the dark-theme design system. Fonts (Playfair Display, DM Mono, DM Sans) are loaded from Google Fonts at runtime.

Key frontend state variables: `allImages`, `filteredImages`, `allTagsCache`, `thumbnails` (map), `multiSelected` (set), `galleryCursor` (keyboard nav index).

Thumbnail loading uses an `IntersectionObserver` (200px root margin) to queue requests for visible cards only, draining three at a time to match the server's `_thumb_semaphore` limit. The browser polls `/api/images/watch` every 2 seconds; on hash change it reconciles `allImages` in place without a full page reload.
