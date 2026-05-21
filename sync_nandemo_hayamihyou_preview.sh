#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
HTML_PATH="$ROOT_DIR/shortcodes/nandemo/aga-hayamihyou-preview.html"
SITE_CONFIG="$ROOT_DIR/sites/nandemo.json"
POST_ID="24766"

cd "$ROOT_DIR"

.venv/bin/python wp_post.py \
  --post-id "$POST_ID" \
  --html "$HTML_PATH" \
  --site "$SITE_CONFIG"
