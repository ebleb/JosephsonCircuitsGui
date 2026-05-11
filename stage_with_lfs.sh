#!/usr/bin/env bash
set -euo pipefail

# Default threshold: 50 MB, matching GitHub's recommended max file size.
# Usage:
#   ./stage_with_lfs.sh
#   ./stage_with_lfs.sh 100   # use 100 MB threshold instead

THRESHOLD_MB="${1:-50}"
THRESHOLD_BYTES=$((THRESHOLD_MB * 1024 * 1024))

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is not installed."
  exit 1
fi

if ! command -v git-lfs >/dev/null 2>&1; then
  echo "Error: git-lfs is not installed."
  echo "Install it first: https://git-lfs.com"
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

git lfs install

echo "Scanning Git-visible files larger than ${THRESHOLD_MB} MB..."
echo

large_count=0

get_size_bytes() {
  if stat -c%s "$1" >/dev/null 2>&1; then
    stat -c%s "$1"      # Linux
  else
    stat -f%z "$1"      # macOS
  fi
}

# Tracked + untracked files, excluding ignored files.
while IFS= read -r -d '' file; do
  [ -f "$file" ] || continue

  size_bytes="$(get_size_bytes "$file")"

  if [ "$size_bytes" -ge "$THRESHOLD_BYTES" ]; then
    echo "LFS tracking: $file ($(awk "BEGIN {printf \"%.2f\", $size_bytes/1024/1024}") MB)"
    git lfs track "$file"
    large_count=$((large_count + 1))
  fi
done < <(git ls-files -co --exclude-standard -z)

echo
echo "Large files tracked with Git LFS: $large_count"

# Stage .gitattributes first so large files are staged as LFS pointers.
if [ -f ".gitattributes" ]; then
  git add .gitattributes
fi

git add -A

echo
echo "Done. Current staged status:"
git status --short