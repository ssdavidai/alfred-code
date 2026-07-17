#!/usr/bin/env bash
set -euo pipefail

ROOT="${ALFRED_CODE_HOME:-$HOME/.claude/alfred-code}"
TARGET_BIN="$HOME/.claude/bin"
CONFIG="$HOME/.config/alfred-code/controller.toml"
STATE="$HOME/.alfred-code-state-v2"
PLIST="$HOME/Library/LaunchAgents/com.ssdavidai.alfred-code-controller-v2.plist"

mkdir -p "$TARGET_BIN" "$(dirname "$CONFIG")" "$STATE" "$(dirname "$PLIST")"
chmod +x "$ROOT/bin/alfred-code"
chmod +x "$ROOT/bin/alfred-code-daemon"
chmod +x "$ROOT/bin/alfred-code-agent"
chmod +x "$ROOT/bin/alfred-code-agent-guard"
chmod +x "$ROOT/bin/alfred-code-npm-shell"
ln -sf "$ROOT/bin/alfred-code" "$TARGET_BIN/alfred-code"
ln -sf "$ROOT/bin/alfred-code-daemon" "$TARGET_BIN/alfred-code-daemon"
ln -sf "$ROOT/bin/alfred-code-agent" "$TARGET_BIN/alfred-code-agent"
ln -sf "$ROOT/bin/alfred-code-agent-guard" "$TARGET_BIN/alfred-code-agent-guard"
ln -sf "$ROOT/bin/alfred-code-npm-shell" "$TARGET_BIN/alfred-code-npm-shell"
"$TARGET_BIN/alfred-code" --config "$CONFIG" init
if find "$HOME/.superset/host" -mindepth 2 -maxdepth 2 -name host.db -print -quit 2>/dev/null | grep -q .; then
  "$TARGET_BIN/alfred-code" --config "$CONFIG" agents-provision
fi

python3 - "$ROOT/launchd/com.ssdavidai.alfred-code-controller-v2.plist.template" "$PLIST" "$HOME" <<'PY'
import os
import pathlib
import sys

source, target, home = map(pathlib.Path, sys.argv[1:])
content = source.read_text().replace("__HOME__", str(home))
temporary = target.with_suffix(".tmp")
temporary.write_text(content)
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY

plutil -lint "$PLIST"
echo "installed disabled-by-default controller at $PLIST"
echo "run: $TARGET_BIN/alfred-code --config $CONFIG doctor"
echo "load only after reviewing diagnostics: launchctl bootstrap gui/$(id -u) $PLIST"
