#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OLD=$(lsof -ti :8764 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "Stopping old hub (PID $OLD)..."
  kill $OLD
  sleep 1
fi
source ~/gallery-env/bin/activate
cd "$SCRIPT_DIR"
python3 hub.py "$@"
