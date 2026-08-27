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
import shlex
import sys

settings_path = os.environ["SETTINGS"]
plantor = os.environ["PLANTOR"]
timeout = int(os.environ["TIMEOUT"])
# Quote the path: this string is written into settings.json and executed
# through a shell on every ExitPlanMode. An unquoted path containing a space
# ("~/Documents/Claude Code/plantor") splits into separate argv entries and the
# hook silently fails -- and because plantor fails closed, you would never see
# an error, just the built-in dialog.
command = "python3 %s" % shlex.quote(plantor)

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
except ValueError:
    sys.exit("error: %s is not valid JSON; not touching it" % settings_path)
except OSError as exc:
    sys.exit("error: could not read %s: %s" % (settings_path, exc))

# Validate every level before mutating. setdefault only fills in a *missing*
# key, so a malformed existing value would otherwise reach list.setdefault or a
# slice assignment on a dict and surface as a raw traceback mid-install.
if not isinstance(settings, dict):
    sys.exit("error: %s is not a JSON object" % settings_path)

hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.exit("error: %s: .hooks is not an object" % settings_path)

events = hooks.setdefault("PermissionRequest", [])
if not isinstance(events, list):
    sys.exit("error: %s: .hooks.PermissionRequest is not an array" % settings_path)

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
