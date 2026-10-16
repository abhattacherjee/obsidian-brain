#!/bin/bash
# Usage: ./scripts/bump-version.sh <major|minor|patch|X.Y.Z>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
exec python3 "$SCRIPT_DIR/dev-test/version_sync.py" "${1:?Expected major, minor, patch or X.Y.Z}" --root "$PROJECT_ROOT"
