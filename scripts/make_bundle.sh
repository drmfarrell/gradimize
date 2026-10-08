#!/usr/bin/env bash
# Package the repo as a git bundle for offline transfer (e.g. to a server
# with no direct access to the origin remote).
#
# Usage: scripts/make_bundle.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
    echo "Working tree is not clean — commit or stash changes before bundling."
    exit 1
fi

OUT="../gradimize-$(date +%Y%m%d).bundle"
# HEAD must be in the bundle or `git clone <bundle>` has no ref to check out.
git bundle create "$OUT" HEAD main
git bundle verify "$OUT"

echo
echo "Bundle created: $OUT"
echo
echo "To restore on another machine:"
echo "  git clone $OUT gradimize && cd gradimize && git remote remove origin"
echo "Then run scripts/bootstrap.sh to set up the environment."
