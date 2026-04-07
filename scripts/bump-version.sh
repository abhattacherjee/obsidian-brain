#!/bin/bash
# bump-version.sh — Semantic versioning (targets .claude-plugin/plugin.json)
# Usage: ./scripts/bump-version.sh <major|minor|patch|X.Y.Z>
#
# Installed by /harden-repo
# Version file targets customized during installation.

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

print_info()    { echo -e "${BLUE}info${NC} $1"; }
print_success() { echo -e "${GREEN}success${NC} $1"; }
print_warning() { echo -e "${YELLOW}warning${NC} $1"; }
print_error()   { echo -e "${RED}error${NC} $1"; }

show_usage() {
  echo "Usage: $0 <major|minor|patch|X.Y.Z>"
  echo ""
  echo "  major    Bump major version (1.0.0 -> 2.0.0)"
  echo "  minor    Bump minor version (1.0.0 -> 1.1.0)"
  echo "  patch    Bump patch version (1.0.0 -> 1.0.1)"
  echo "  X.Y.Z    Set specific version"
}

TYPE=$1
if [[ -z "$TYPE" ]]; then
  print_error "Missing version type"
  show_usage
  exit 1
fi

# ══════════════════════════════════════════════════════════════
# VERSION FILES — customized by /harden-repo during installation
# ══════════════════════════════════════════════════════════════

VERSION_SOURCE="$PROJECT_ROOT/.claude-plugin/plugin.json"

# ── Read current version ──────────────────────────────────────

read_version() {
  local file="$1"
  case "$file" in
    *.json)
      python3 -c "import json; print(json.load(open('$file'))['version'])" 2>/dev/null || \
      node -e "console.log(require('$file').version)" 2>/dev/null
      ;;
    *pyproject.toml)
      grep -m1 '^version\s*=' "$file" | sed 's/.*"\(.*\)".*/\1/'
      ;;
    *Cargo.toml)
      awk '/^\[package\]/,/^\[/' "$file" | grep -m1 '^version\s*=' | sed 's/.*"\(.*\)".*/\1/'
      ;;
    *version.txt)
      tr -d '[:space:]' < "$file"
      ;;
  esac
}

CURRENT_VERSION=$(read_version "$VERSION_SOURCE")
if [[ -z "$CURRENT_VERSION" ]]; then
  print_error "Could not read version from $VERSION_SOURCE"
  exit 1
fi
print_info "Current version: $CURRENT_VERSION"

# ── Calculate new version ─────────────────────────────────────

IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

case "$TYPE" in
  major) NEW_VERSION="$((MAJOR + 1)).0.0" ;;
  minor) NEW_VERSION="$MAJOR.$((MINOR + 1)).0" ;;
  patch) NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))" ;;
  *)
    if [[ ! "$TYPE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      print_error "Invalid version format: $TYPE (expected X.Y.Z)"
      exit 1
    fi
    NEW_VERSION="$TYPE"
    ;;
esac

print_info "New version: $NEW_VERSION"

# ── Update version files ─────────────────────────────────────

update_version() {
  local file="$1"
  local version="$2"

  if [[ ! -f "$file" ]]; then
    print_warning "Skipping $file (not found)"
    return
  fi

  case "$file" in
    *.json)
      if command -v python3 &>/dev/null; then
        python3 -c "
import json, pathlib
p = pathlib.Path('$file')
d = json.loads(p.read_text())
d['version'] = '$version'
p.write_text(json.dumps(d, indent=2) + '\n')
"
      elif command -v node &>/dev/null; then
        node -e "
          const fs = require('fs');
          const pkg = JSON.parse(fs.readFileSync('$file', 'utf-8'));
          pkg.version = '$version';
          fs.writeFileSync('$file', JSON.stringify(pkg, null, 2) + '\n');
        "
      else
        print_error "Need python3 or node to update JSON files"
        exit 1
      fi
      ;;
    *pyproject.toml)
      sed -i.bak "s/^version\s*=\s*\".*\"/version = \"$version\"/" "$file" && rm -f "${file}.bak"
      ;;
    *Cargo.toml)
      python3 -c "
import re, pathlib
p = pathlib.Path('$file')
content = p.read_text()
content = re.sub(
    r'(^\[package\].*?^version\s*=\s*)\"[^\"]+\"',
    r'\g<1>\"$version\"',
    content, count=1, flags=re.MULTILINE | re.DOTALL
)
p.write_text(content)
"
      ;;
    *version.txt)
      echo "$version" > "$file"
      ;;
  esac

  print_success "Updated $(basename "$file") to $version"
}

update_version "$PROJECT_ROOT/.claude-plugin/plugin.json" "$NEW_VERSION"

# Keep the marketplace registry pointer in lockstep with plugin.json.
# Without this, /plugin marketplace browse advertises a stale version
# to users even though the plugin itself has been released.
MARKETPLACE_JSON="$PROJECT_ROOT/.claude-plugin/marketplace.json"
if [[ -f "$MARKETPLACE_JSON" ]]; then
  PLUGIN_NAME=$(python3 -c "import json; print(json.load(open('$PROJECT_ROOT/.claude-plugin/plugin.json'))['name'])")
  if ! python3 - "$MARKETPLACE_JSON" "$NEW_VERSION" "$PLUGIN_NAME" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
new_version = sys.argv[2]
plugin_name = sys.argv[3]

try:
    data = json.loads(path.read_text())
except Exception as e:
    sys.stderr.write(f"ERROR: could not parse {path}: {e}\n")
    sys.exit(2)

plugins = data.get("plugins")
if not isinstance(plugins, list) or not plugins:
    sys.stderr.write(f"ERROR: {path} has no 'plugins' array\n")
    sys.exit(2)

matches = [p for p in plugins if p.get("name") == plugin_name]
if not matches:
    sys.stderr.write(
        f"ERROR: marketplace.json has no entry for plugin '{plugin_name}'. "
        f"Add an entry before releasing, or the marketplace listing will "
        f"lie about the version.\n"
    )
    sys.exit(1)

changed = 0
for plugin in matches:
    if plugin.get("version") != new_version:
        plugin["version"] = new_version
        changed += 1

if changed:
    # Atomic write: temp file in same dir + os.replace. Mirrors the
    # write_vault_note() convention in hooks/obsidian_utils.py so a
    # SIGINT or disk-full mid-write cannot corrupt the registry pointer.
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(f"marketplace.json: updated {changed} entry for '{plugin_name}' to {new_version}")
else:
    print(f"marketplace.json: '{plugin_name}' already at {new_version}")
PY
  then
    print_error "Failed to sync marketplace.json — fix the above and re-run"
    exit 1
  fi
  print_success "Synced marketplace.json entry for $PLUGIN_NAME to $NEW_VERSION"
fi

echo ""
print_success "Version bumped from $CURRENT_VERSION to $NEW_VERSION"
echo ""
print_info "Next steps:"
echo "  1. Update CHANGELOG.md with release notes"
echo "  2. Stage version files + CHANGELOG.md"
echo "  3. git commit -m \"chore(release): bump version to $NEW_VERSION\""
