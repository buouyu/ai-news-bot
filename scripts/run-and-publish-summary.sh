#!/usr/bin/env bash
set -euo pipefail

hours="${1:-24}"
branch="$(git branch --show-current)"

if [[ -z "$branch" ]]; then
  echo "Unable to determine current git branch." >&2
  exit 1
fi

uv run horizon --hours "$hours"

git add docs/_posts

if git diff --cached --quiet -- docs/_posts; then
  echo "No docs/_posts changes to publish."
  exit 0
fi

summary_date="$(date +%Y-%m-%d)"
git commit -m "Update daily summary: ${summary_date}" -- docs/_posts
git push origin "$branch"

echo "Published daily summary changes from docs/_posts to origin/${branch}."
