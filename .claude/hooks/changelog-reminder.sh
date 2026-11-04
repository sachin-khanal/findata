#!/usr/bin/env bash
# PostToolUse hook (every Bash call): after a command that can make commits (git commit, merge,
# cherry-pick, revert, pull), ask Claude to add changelog.txt lines for commits it lacks.
# Silent for other commands, and for commits that only changed changelog.txt.
#
# The repo checked is the one the git command ran in (`git -C <dir>`, else the last `cd <dir>`
# before it, else the hook's cwd), so a commit in a worktree is checked against that worktree's
# changelog. The last 50 commits are checked, not just HEAD, so one missed earlier (say, made
# by a subagent whose hook did not fire) is caught by the next commit or merge.
set -u

input=$(cat)
cmd=$(jq -r '.tool_input.command // empty' <<<"$input" 2>/dev/null) || exit 0

# Prints the git command's directory as written (possibly empty); fails if nothing commits.
target=$(printf '%s' "$cmd" | perl -0777 -ne '
  /^(.*?)(?<![\w.\/-])(?:\S*\/)?git\s+(?:-C\s+(\S+)\s+)?(?:commit|merge|cherry-pick|revert|pull)(?![\w-])/s
    or exit 1;
  my ($before, $dir) = ($1, $2);
  ($dir) = ($before =~ /(?:^|[\s;&|(])cd\s+([^\s;&|)]+)/g)[-1] unless defined $dir;
  print $dir // "";
') || exit 0

base=$(jq -r '.cwd // empty' <<<"$input")
base=${base:-$PWD}
dir=$base
if [ -n "$target" ]; then
  target=${target//[\"\']/}
  target=${target/#\~/$HOME}
  target=${target/#\$HOME/$HOME}
  target=${target/#\$\{HOME\}/$HOME}
  case $target in /*) dir=$target ;; *) dir=$base/$target ;; esac
fi
root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) ||
  root=$(git -C "$base" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" && [ -f changelog.txt ] || exit 0
git rev-parse -q --verify HEAD >/dev/null || exit 0

missing=""
for sha in $(git log -n 50 --format=%h); do
  grep -q "^${sha:0:7}" changelog.txt && continue
  if git rev-parse -q --verify "$sha^1" >/dev/null; then
    files=$(git diff --name-only "$sha^1" "$sha") # for a merge: what it brought in
  else
    files=$(git show --name-only --format= "$sha")
  fi
  [ "$files" = "changelog.txt" ] && continue
  missing="  $(git log -1 --format='%h  %ad  %s' --date=short "$sha")
$missing"
done
[ -n "$missing" ] || exit 0

branch=$(git rev-parse --abbrev-ref HEAD)

msg="$root/changelog.txt (branch $branch) has no line for these commits, oldest first:
$missing
Append one line for each, keeping the file oldest-first, in the existing format: '<sha>  <date>  [<branch>]  <one-line description of what changed and why, no trailing period needed>', where <branch> is the branch the commit was made on ($branch for a commit just made; a merge is listed under the branch merged into). Update the 'Uncommitted changes' footer if it is now wrong, and leave changelog.txt uncommitted so it rides along with the next commit (do not make a changelog-only commit)."

jq -n --arg msg "$msg" '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}'
