#!/usr/bin/env bash
#
# Install the plantor hook into ~/.claude/settings.json.
#
# Registers a PermissionRequest hook matched on ExitPlanMode. Backs up the
# existing settings file first. Uses python3 for the JSON edit so there is no
# jq dependency -- if you can run plantor, you can run this.
#
# Re-running is safe: an existing plantor hook is replaced, not duplicated.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANTOR="$REPO_DIR/plantor.py"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

# 4 days. Plantor intentionally does not time a review out on its own; this is
# the only ceiling, and it exists so a forgotten tab cannot wedge a session
# forever.
TIMEOUT=345600

if [ ! -f "$PLANTOR" ]; then
  echo "error: plantor.py not found at $PLANTOR" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

BACKUP="$SETTINGS.bak.$(date +%Y%m%d%H%M%S)"
cp "$SETTINGS" "$BACKUP"

PLANTOR="$PLANTOR" SETTINGS="$SETTINGS" TIMEOUT="$TIMEOUT" python3 <<'PY'
import json
import os
import sys

settings_path = os.environ["SETTINGS"]
plantor = os.environ["PLANTOR"]
timeout = int(os.environ["TIMEOUT"])
command = "python3 %s" % plantor

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
except ValueError:
    sys.exit("error: %s is not valid JSON; not touching it" % settings_path)

if not isinstance(settings, dict):
    sys.exit("error: %s is not a JSON object" % settings_path)

hooks = settings.setdefault("hooks", {})
events = hooks.setdefault("PermissionRequest", [])

entry = {
    "matcher": "ExitPlanMode",
    "hooks": [{"type": "command", "command": command, "timeout": timeout}],
}

# Replace any existing plantor registration rather than stacking another one.
def is_plantor(block):
    for hook in block.get("hooks", []):
        if "plantor" in str(hook.get("command", "")):
            return True
    return False

events[:] = [b for b in events if not is_plantor(b)]
events.append(entry)

with open(settings_path, "w") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\n")

print("registered: %s" % command)
PY

echo "backup:     $BACKUP"
echo "settings:   $SETTINGS"
echo
echo "Done. Start a new Claude Code session, enter plan mode, and the review"
echo "UI will open when a plan is presented."
