#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Secure Gallery — First Run Setup
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/.gallery-config"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       Secure Gallery — First Run         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── DETECT ENVIRONMENT ────────────────────────────────────────────────────────
if grep -qi microsoft /proc/version 2>/dev/null; then
  ENV="wsl"
  ENV_LABEL="WSL2 (Windows)"
  DEFAULT_DROP="/mnt/c/photodrop"
elif [ -d "/mnt/chromeos" ]; then
  ENV="chromeos"
  ENV_LABEL="ChromeOS (Crostini)"
  DEFAULT_DROP="/mnt/chromeos/MyFiles/Downloads/photodrop"
else
  ENV="linux"
  ENV_LABEL="Linux"
  DEFAULT_DROP="$HOME/photodrop"
fi

echo "Detected environment: $ENV_LABEL"
echo ""

# ── STEP 1: DEPENDENCIES ──────────────────────────────────────────────────────
echo "Step 1 — Checking dependencies"
echo "─────────────────────────────────"

MISSING_APT=()

if ! command -v python3 &>/dev/null; then
  echo "  ✘  python3 not found"
  MISSING_APT+=(python3 python3-pip)
else
  echo "  ✓  python3 $(python3 --version 2>&1 | cut -d' ' -f2)"
fi

if ! command -v gocryptfs &>/dev/null; then
  echo "  ✘  gocryptfs not found (needed for encrypted vaults)"
  MISSING_APT+=(gocryptfs)
else
  echo "  ✓  gocryptfs $(gocryptfs --version 2>&1 | head -1 | cut -d' ' -f2)"
fi

if ! command -v lsof &>/dev/null; then
  echo "  ✘  lsof not found"
  MISSING_APT+=(lsof)
else
  echo "  ✓  lsof"
fi

if ! command -v fusermount &>/dev/null && ! command -v fusermount3 &>/dev/null; then
  echo "  ✘  fusermount not found"
  MISSING_APT+=(fuse3)
else
  echo "  ✓  fusermount"
fi

if [ ${#MISSING_APT[@]} -gt 0 ]; then
  echo ""
  echo "  Installing missing packages: ${MISSING_APT[*]}"
  sudo apt-get install -y "${MISSING_APT[@]}" || {
    echo "  Installation failed. Please run:"
    echo "    sudo apt install ${MISSING_APT[*]}"
    exit 1
  }
fi

echo ""

# ── STEP 2: PYTHON ENVIRONMENT ────────────────────────────────────────────────
echo "Step 2 — Python virtual environment"
echo "─────────────────────────────────────"

VENV_DIR="$HOME/gallery-env"
if [ -d "$VENV_DIR" ]; then
  echo "  ✓  Virtual environment already exists at $VENV_DIR"
else
  echo "  Creating virtual environment at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR" || { echo "Failed to create venv."; exit 1; }
  echo "  ✓  Created"
fi

source "$VENV_DIR/bin/activate"

echo "  Installing Python packages..."
pip install -q Pillow piexif imagehash watchdog pyzipper && echo "  ✓  Packages installed"
echo ""

# ── STEP 3: DROP FOLDER ───────────────────────────────────────────────────────
echo "Step 3 — Drop folder"
echo "─────────────────────"
echo "  The drop folder is where you copy photos to add them to a gallery."
echo "  Each gallery gets its own subfolder inside it."
echo ""
echo "  Suggested path for $ENV_LABEL:"
echo "    $DEFAULT_DROP"
echo ""
read -rp "  Enter drop folder path [press Enter for default]: " USER_DROP
DROP_SOURCE="${USER_DROP:-$DEFAULT_DROP}"

# Create base drop folder
if mkdir -p "$DROP_SOURCE" 2>/dev/null; then
  echo "  ✓  Drop folder ready: $DROP_SOURCE"
else
  echo "  ✘  Could not create $DROP_SOURCE"
  echo "     Please create it manually, then re-run firstrun.sh"
  exit 1
fi

# Save to config
echo "DROP_SOURCE=\"$DROP_SOURCE\"" > "$CONFIG_FILE"
echo "  ✓  Saved to $CONFIG_FILE"
echo ""

# ── STEP 4: FIRST GALLERY ─────────────────────────────────────────────────────
echo "Step 4 — Create your first gallery"
echo "────────────────────────────────────"
echo ""
read -rp "  Create a gallery now? [Y/n]: " CREATE_NOW
CREATE_NOW="${CREATE_NOW:-Y}"

if [[ "$CREATE_NOW" =~ ^[Yy] ]]; then
  echo ""
  read -rp "  Gallery name (letters, numbers, hyphens): " GALLERY_NAME
  GALLERY_NAME="${GALLERY_NAME:-my-gallery}"

  read -rp "  Description (optional): " GALLERY_DESC

  echo ""
  bash "$SCRIPT_DIR/new-gallery.sh" "$GALLERY_NAME" "$GALLERY_DESC"
fi

# ── DONE ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║              Setup complete!             ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  Read the cheat-sheet for full instructions:"
echo "    cat $SCRIPT_DIR/ReallyREADME.md"
echo ""
echo "  Start a gallery:"
echo "    $SCRIPT_DIR/start-gallery.sh <name>"
echo ""
echo "  Start the gallery hub (manage all galleries):"
echo "    $SCRIPT_DIR/start-hub.sh"
echo "    → http://localhost:8764"
echo ""
