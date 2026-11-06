# Working on findata

Instructions for every agent and developer working in this repo. What findata is: `README.md`.
Setup, configuration and commands: `docs/DEV_SETUP.txt`. Why the design is what it is:
`docs/adr/`; read the relevant ADR before changing something it decides.

## Workspace

- **Virtualenv outside the repo.** Set `UV_PROJECT_ENVIRONMENT="$HOME/.venvs/findata"`, and a
  separate `~/.venvs/findata-<topic>` per worktree. The repo sits in a synced Desktop folder
  that hides dot-folders, which breaks an in-repo `.venv` (DEV_SETUP section 7b).
- **The repo holds findata only.** Install tools and scaffold experiments (another framework's
  starter project, a scratch app) in their own directories outside it. When a tool insists on
  writing state into the repo, add its path to the "Dev tooling" block of `.gitignore` in the
  same change.
- **Test data stays separate.** `~/.findata` is the default data directory and can hold real
  bank data. Experiment with `FINDATA_DATA_DIR=~/.findata-sandbox` and Plaid Sandbox keys.

## Git

- Branch from `main` for every change (`feature/`, `fix/`, `docs/`, `refactor/`, `plan/`,
  `design/`) and merge back with `git merge --no-ff`, message
  `Merge <branch>: <what it brings>`. Larger work gets a worktree at
  `~/.worktrees/findata-<topic>`.
- Agents commit when the user asks. Pre-commit runs ruff and secret checks; fix what it reports.
- Done means `uv run ruff check`, `uv run ruff format --check` and `uv run pytest -q` all pass.

## changelog.txt

One line per commit, oldest first: `<sha>  <date>  [<branch>]  <what changed and why>`. A
merge is listed under the branch it was merged into. Claude Code gets a reminder from
`.claude/hooks/changelog-reminder.sh`; other agents add the line themselves after each commit.
Leave the changelog edit uncommitted so it rides along with the next commit, and keep the two
footer lines (`Uncommitted changes`, `Branch notes`) true.

## Running things

- Run servers, the daemon, `sandbox-link` and test suites in a tmux session the user can watch:
  `tmux new-session -d -s findata-<name> -c <repo>`, then open it with
  `osascript -e 'tell application "Terminal" to do script "tmux attach -t findata-<name>"'`.
  Quick read-only commands can run directly.
- **Clean up when the task ends:** stop every process you started and `tmux kill-session` each
  `findata-*` session you created.

## Multi-task builds

A plan in `docs/superpowers/plans/` is built task by task: tick its checkboxes as tasks
finish. Working notes (briefs, reports, rulings, review diffs) live in
`.superpowers/sdd/<plan>/progress.md` inside the worktree, which is git-ignored. Record the
outcome of every agent you dispatch there; a dispatch with no recorded outcome is unfinished
work.
