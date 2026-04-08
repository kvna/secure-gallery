#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME=$1
DESC="${2:-}"
NO_VAULT=false

for arg in "$@"; do
  [ "$arg" = "--no-vault" ] && NO_VAULT=true
done

if [ -z "$NAME" ] || [[ "$NAME" == --* ]]; then
  echo "Usage: new-gallery.sh <gallery-name> [description] [--no-vault]"
  exit 1
fi

if ! [[ "$NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "Name must contain only letters, numbers, hyphens, or underscores."
  exit 1
fi

GALLERY_DIR="$SCRIPT_DIR/galleries/$NAME"

if [ -d "$GALLERY_DIR" ]; then
  echo "Gallery '$NAME' already exists at $GALLERY_DIR"
  exit 1
fi

# Read drop source from config
DROP_SOURCE="/mnt/c/photodrop"
[ -f "$SCRIPT_DIR/.gallery-config" ] && source "$SCRIPT_DIR/.gallery-config"

# ── VAULT SETUP ────────────────────────────────────────────────────────────
if ! $NO_VAULT; then
  if ! command -v gocryptfs &>/dev/null; then
    echo "gocryptfs not found. Install with:"
    echo "  sudo apt install gocryptfs"
    echo ""
    echo "Or create without encryption:"
    echo "  new-gallery.sh $NAME ${DESC:+"$DESC" }--no-vault"
    exit 1
  fi

  mkdir -p "$GALLERY_DIR/.vault"
  mkdir -p "$GALLERY_DIR/vault"

  echo "Initializing encrypted vault for '$NAME'."
  echo "You will be prompted to set a password."
  echo ""
  gocryptfs -init "$GALLERY_DIR/.vault" || {
    echo "Vault initialisation failed."
    rm -rf "$GALLERY_DIR"
    exit 1
  }
  VAULT_FLAG=true
else
  mkdir -p "$GALLERY_DIR/photos"
  VAULT_FLAG=false
fi
# ───────────────────────────────────────────────────────────────────────────

# Auto-assign next available port starting from 8765
PORT=8765
while true; do
  TAKEN=false
  for f in "$SCRIPT_DIR/galleries/"/*/gallery.json; do
    [ -f "$f" ] || continue
    USED=$(python3 -c "import json; print(json.load(open('$f')).get('port', 0))" 2>/dev/null)
    [ "$USED" = "$PORT" ] && TAKEN=true && break
  done
  $TAKEN || break
  PORT=$((PORT + 1))
done

cat > "$GALLERY_DIR/gallery.json" <<JSONEOF
{
  "name": "$NAME",
  "description": "$DESC",
  "created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "port": $PORT,
  "vault": $VAULT_FLAG
}
JSONEOF

echo ""
echo "Created gallery '$NAME'"
if ! $NO_VAULT; then
  echo "  Ciphertext : $GALLERY_DIR/.vault/"
  echo "  Mountpoint : $GALLERY_DIR/vault/  (empty until unlocked)"
else
  echo "  Photos     : $GALLERY_DIR/photos/"
fi
echo "  Port       : $PORT  →  http://localhost:$PORT"
echo "  Drop folder: $DROP_SOURCE/$NAME"
echo ""
echo "Start with : $SCRIPT_DIR/start-gallery.sh $NAME"
