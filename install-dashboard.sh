#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.ssdavidai.alfred-code-dashboard"
CONFIG="${ALFRED_CODE_CONFIG:-$HOME/.config/alfred-code/controller.toml}"
STATE_DIR="${ALFRED_CODE_STATE_DIR:-$HOME/.alfred-code-state-v2}"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$ROOT/launchd/$LABEL.plist.template"
BIN="$ROOT/bin/alfred-code"

mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR"
sed \
  -e "s|__ALFRED_CODE_BIN__|$BIN|g" \
  -e "s|__ALFRED_CODE_CONFIG__|$CONFIG|g" \
  -e "s|__ALFRED_CODE_STATE__|$STATE_DIR|g" \
  -e "s|__ALFRED_CODE_ROOT__|$ROOT|g" \
  -e "s|__HOME__|$HOME|g" \
  "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Alfred Operations is running at http://127.0.0.1:7331"
