#!/usr/bin/env bash
# PostToolUse hook (Bash, `git commit *`): ask Claude to add a one-line entry for the new
# commit to changelog.txt. Silent when HEAD is already listed or only changed changelog.txt.
set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

sha=$(git rev-parse --short HEAD 2>/dev/null) || exit 0
[ -f changelog.txt ] && grep -q "^$sha " changelog.txt && exit 0

files=$(git show --name-only --format= HEAD)
[ "$files" = "changelog.txt" ] && exit 0

subject=$(git log -1 --format=%s HEAD)
date=$(git log -1 --format=%ad --date=short HEAD)
branch=$(git rev-parse --abbrev-ref HEAD)

msg="A commit was just made: $sha ($date) on $branch: \"$subject\". Append exactly one line for it to changelog.txt in the existing format: '$sha  $date  [$branch]  <one-line description of what changed and why, no trailing period needed>'. Keep entries oldest-first, update the 'Uncommitted changes' footer if it is now wrong, and leave changelog.txt uncommitted so it rides along with the next commit (do not make a changelog-only commit)."

jq -n --arg msg "$msg" '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}'
