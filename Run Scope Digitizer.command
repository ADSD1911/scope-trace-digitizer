#!/bin/bash
# Double-click this file to start the Scope Trace Digitizer. It opens the page in your
# browser. Keep this window open while you use the page; close it to stop.
cd "$(dirname "$0")" || exit 1

# Use the first Python that has the libraries the tool needs.
for PY in python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if "$PY" -c "import numpy, PIL, matplotlib" 2>/dev/null; then FOUND="$PY"; break; fi
done
if [ -z "$FOUND" ]; then
  echo "No Python with numpy, Pillow and matplotlib was found."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
"$FOUND" scope_web.py "$@"
