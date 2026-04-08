#!/usr/bin/env python3
"""
Monitor a source folder and move files to a gallery's photos directory.

Normal interval : 60s
Fast interval   : 5s  (triggered when new files are found)
Fast mode exits : after 60s with no new files

Usage:
  python3 monitor.py --gallery <name>              # watches C:\photodrop\<name>
  python3 monitor.py --source /some/path --dest /other/path
"""

import argparse
import logging
import os
import shutil
import time
from pathlib import Path

DEFAULT_SOURCE = Path("/mnt/c/photodrop")
DEFAULT_DEST   = Path(__file__).parent / 'photos'
GALLERIES_DIR  = Path(__file__).parent / 'galleries'

NORMAL_INTERVAL   = 60   # seconds between checks normally
FAST_INTERVAL     = 5    # seconds between checks in fast mode
FAST_MODE_TIMEOUT = 60   # seconds of quiet before reverting to normal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def move_files(source: Path, dest: Path) -> int:
    """Move all files from source to dest. Returns number of files moved."""
    if not source.exists():
        log.warning("Source not found: %s", source)
        return 0

    moved = 0
    for entry in source.iterdir():
        if not entry.is_file():
            continue

        target = dest / entry.name
        if target.exists():
            stem, suffix = entry.stem, entry.suffix
            n = 2
            while target.exists():
                target = dest / f"{stem}-{n}{suffix}"
                n += 1

        try:
            shutil.move(str(entry), str(target))
            log.info("Moved  %s  →  %s", entry.name, target.name)
            moved += 1
        except OSError as e:
            log.error("Failed to move %s: %s", entry.name, e)

    return moved


def main() -> None:
    p = argparse.ArgumentParser(description='Move files from source to gallery photos folder.')
    p.add_argument('--gallery', '-g', default=None,
                   help='Gallery name — sends files to ~/ns/galleries/<name>/photos')
    p.add_argument('--source', '-s', default=None,
                   help=f'Source folder (default: {DEFAULT_SOURCE})')
    p.add_argument('--dest', '-d', default=None,
                   help='Destination folder (overridden by --gallery)')
    a = p.parse_args()

    if a.source:
        source = Path(a.source)
    elif a.gallery:
        source = DEFAULT_SOURCE / a.gallery   # C:\photodrop\<gallery-name>
    else:
        source = DEFAULT_SOURCE

    if a.gallery:
        gallery_dir = GALLERIES_DIR / a.gallery
        # Vault galleries store photos inside the gocryptfs mountpoint
        dest = gallery_dir / 'vault' / 'photos' if (gallery_dir / '.vault').exists() \
               else gallery_dir / 'photos'
    elif a.dest:
        dest = Path(a.dest)
    else:
        dest = DEFAULT_DEST

    dest.mkdir(parents=True, exist_ok=True)
    log.info("Source : %s", source)
    log.info("Dest   : %s", dest)
    log.info("Ready  (normal interval %ds, fast interval %ds)", NORMAL_INTERVAL, FAST_INTERVAL)

    fast_mode          = False
    last_new_file_time = 0.0

    while True:
        moved = move_files(source, dest)
        now   = time.monotonic()

        if moved > 0:
            last_new_file_time = now
            if not fast_mode:
                fast_mode = True
                log.info("Fast mode ON  (checking every %ds)", FAST_INTERVAL)
        elif fast_mode and (now - last_new_file_time) >= FAST_MODE_TIMEOUT:
            fast_mode = False
            log.info("Fast mode OFF (reverting to every %ds)", NORMAL_INTERVAL)

        time.sleep(FAST_INTERVAL if fast_mode else NORMAL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
