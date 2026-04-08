#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GALLERY_NAME=$1
if [ -z "$GALLERY_NAME" ] || [[ "$GALLERY_NAME" == --* ]]; then
  echo "Usage: start-gallery.sh <gallery-name> [--minimal] [--port N] [--zip file.zip]"
  echo ""
  echo "Available galleries:"
  ls "$SCRIPT_DIR/galleries/" 2>/dev/null | sed 's/^/  /' || echo "  (none — run: new-gallery.sh <name>)"
  exit 1
fi

shift   # remaining args go to server.py

GALLERY_DIR="$SCRIPT_DIR/galleries/$GALLERY_NAME"
VAULT_DIR="$GALLERY_DIR/.vault"
MOUNT_DIR="$GALLERY_DIR/vault"
PORT=8765
GALLERY_PASSWORD=""

# Read drop source from config (written by firstrun.sh)
DROP_SOURCE="/mnt/c/photodrop"
[ -f "$SCRIPT_DIR/.gallery-config" ] && source "$SCRIPT_DIR/.gallery-config"

# Read port from gallery.json if present
if [ -f "$GALLERY_DIR/gallery.json" ]; then
  JSON_PORT=$(python3 -c "import json; d=json.load(open('$GALLERY_DIR/gallery.json')); print(d.get('port',''))" 2>/dev/null)
  [ -n "$JSON_PORT" ] && PORT=$JSON_PORT
fi

# Allow --port / -p override in remaining args
HAS_PORT=false
PREV=""
for arg in "$@"; do
  if [ "$PREV" = "--port" ] || [ "$PREV" = "-p" ]; then
    PORT=$arg; HAS_PORT=true
  fi
  PREV=$arg
done

# ── VAULT ──────────────────────────────────────────────────────────────────
if [ -d "$VAULT_DIR" ]; then
  if ! command -v gocryptfs &>/dev/null; then
    echo "gocryptfs not found. Install: sudo apt install gocryptfs"
    exit 1
  fi

  mkdir -p "$MOUNT_DIR"

  if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    echo "Vault already mounted."
  else
    read -s -p "Password for '$GALLERY_NAME': " GALLERY_PASSWORD
    echo
    echo "$GALLERY_PASSWORD" | gocryptfs -passfile /dev/stdin "$VAULT_DIR" "$MOUNT_DIR" \
      || { echo "Failed to unlock vault."; exit 1; }
    echo "Vault unlocked."
  fi

  cleanup() {
    echo ""
    [ -n "$MONITOR_PID" ] && kill "$MONITOR_PID" 2>/dev/null && echo "Monitor stopped."
    echo "Locking vault..."
    fusermount -u "$MOUNT_DIR" 2>/dev/null \
      && echo "Vault locked." \
      || echo "Note: unmount failed (may already be unmounted)."
  }
  trap cleanup EXIT

  PHOTOS_DIR="$MOUNT_DIR/photos"
else
  PHOTOS_DIR="$GALLERY_DIR/photos"
fi
# ───────────────────────────────────────────────────────────────────────────

OLD=$(lsof -ti :$PORT 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "Stopping old server on port $PORT (PID $OLD)..."
  kill $OLD
  sleep 1
fi

mkdir -p "$PHOTOS_DIR"
source ~/gallery-env/bin/activate
cd "$SCRIPT_DIR"
export GALLERY_PASSWORD

# Start drop-folder monitor in background
MONITOR_PID=""
DROP_DIR="$DROP_SOURCE/$GALLERY_NAME"
if [ -d "$DROP_SOURCE" ]; then
  mkdir -p "$DROP_DIR" 2>/dev/null || true
  python3 monitor.py --source "$DROP_DIR" --gallery "$GALLERY_NAME" &
  MONITOR_PID=$!
  echo "Monitor started — drop files into $DROP_DIR (PID $MONITOR_PID)"
else
  echo "Note: drop folder '$DROP_SOURCE' not found — monitor not started."
fi

if $HAS_PORT; then
  python3 server.py --folder "$PHOTOS_DIR" "$@"
else
  python3 server.py --folder "$PHOTOS_DIR" --port $PORT "$@"
fi
