#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 server.py --open
else
  echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Enter to close..." _
fi
