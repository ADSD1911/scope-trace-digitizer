#!/bin/bash
# Double-click to try the link version on this Mac, exactly as it will run once uploaded.
# It serves the "docs" folder at http://127.0.0.1:8740/ and opens it. Close this window to stop.
cd "$(dirname "$0")/docs" || exit 1
( sleep 1; open "http://127.0.0.1:8740/" ) &
python3 -m http.server 8740 --bind 127.0.0.1
