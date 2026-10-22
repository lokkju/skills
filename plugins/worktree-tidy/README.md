# worktree-tidy

Worktrees made for agent and PR work pile up after their PRs merge. Claude Code removes the ones
`EnterWorktree` made when a session exits cleanly, but nothing removes the ones `git worktree add`
made, and agents can't run `git worktree remove --force`, which repos with submodules need. One
sweep of my codexpublicus repos removed 67 of them.

worktree-tidy finds them at session start and tells you. It never removes anything: it prints
the commands, and you run them.

## What you see

When something is stale, the session starts with one line:

```
worktree-tidy: 28 stale worktrees (27 merged, 1 detached) in skills; 15 to look at.
/worktree-tidy lists them with the commands to remove them (last list: ~/.cache/…/skills-….txt).
```

Claude gets the same line, plus an instruction to leave removal to you. Nothing is printed when
nothing is stale. `/worktree-tidy` (or asking Claude about stale worktrees) runs the full report:

```
skills (/home/me/projects/skills): 48 linked worktrees
  stale detached: .claude/worktrees/app-create: detached at 25b73c4fc, unused 8 days
  stale merged: .claude/worktrees/agent-a28de… [feat/forge-tracker-blocking-links]: PR #92 merged
  look at: .claude/worktrees/agent-a23f3… [feat/sd-forge-hosts]: PR #57 merged; branch still on the remote
```

followed by one shell line per stale worktree, for you to paste or run with `!`:

```
{ [ -z "$(git -C <worktree> status --porcelain --ignore-submodules=none)" ] && git -C <repo> worktree remove <worktree> && git -C <repo> branch -D <branch>; } || echo 'worktree-tidy: left <worktree> (not clean, or git refused)' >&2
git -C <repo> worktree prune
```

Every line checks that the worktree is still clean before it removes it, so running them an hour
after the report can't discard work done since. `--force` appears only for a clean worktree that
has a `.gitmodules`, because git refuses to remove those without it.

## What counts as stale

- **merged** or **closed**: the branch's newest pull request merged (or closed unmerged), no PR
  from the branch is open, the branch is gone from the GitHub remote, the worktree's commit is in
  the PR, and `git status --porcelain --ignore-submodules=none` is empty.
- **detached**: a detached checkout that's clean, on a commit some branch or tag has, and unused
  (its HEAD, index and reflog untouched) for 7 days.
- **prunable**: git already marks it prunable because its directory is gone.

"Look at" lists what is close but not safe to call stale: a merged worktree with uncommitted
changes, commits the PR doesn't have, or its branch still on the remote; a detached checkout
that's dirty or on a commit no branch has; and directories under `.claude/worktrees/` or `.wt/`
that git doesn't list as worktrees. These get no commands.

It never lists a repo's main checkout, the worktree the session runs in, a locked worktree, or a
branch with an open PR or no PR at all.

## Where it looks

The session's repo (every worktree `git worktree list` shows, wherever it lives), or, when the
session starts in a folder that isn't a git repo, every non-hidden git repo one level down. Set
`WORKTREE_TIDY_ROOTS` (paths separated by `:`) to scan a fixed list instead; each entry is a
repo or a folder of repos.

## GitHub, the cache and the time budget

PR state and branch presence come from one read-only `gh api graphql` query per repo, covering
all its worktree branches, and each answer is cached for ten minutes under
`$CLAUDE_PLUGIN_DATA` (or `~/.cache/worktree-tidy`). If gh is missing, not logged in, or fails,
the repo's branch worktrees are reported as unchecked, never as gone. A remote that isn't on
GitHub, or sits behind an SSH host alias, can be named with
`git config worktree-tidy.repo owner/name` (or `host/owner/name` for GitHub Enterprise).

The hook stops after 4 seconds and says what it didn't reach; the hook's own timeout is 15. A
warm cache makes it about 0.2 seconds on a repo with 48 worktrees.

| Variable | Default | |
| --- | --- | --- |
| `WORKTREE_TIDY_ROOTS` | the session's directory | repos or folders of repos to scan |
| `WORKTREE_TIDY_BUDGET` | `4` | seconds the hook may spend |
| `WORKTREE_TIDY_DETACHED_DAYS` | `7` | how long a detached checkout sits unused before it's stale |
| `WORKTREE_TIDY_TTL` | `600` | seconds a GitHub answer is reused |
| `WORKTREE_TIDY_OFFLINE` | unset | `1` skips GitHub; only detached and prunable worktrees can be stale |

Put them in the `env` block of your Claude Code settings. The report takes `--root`, `--days`,
`--offline`, `--refresh` (ignore the cache) and `--json` directly:

```bash
plugins/worktree-tidy/scripts/worktree-tidy report --root ~/projects/codexpublicus --refresh
```

Python 3.9 or newer, git 2.36 or newer, and `gh` for the PR check. Tests:
`uv run --no-project --with pytest pytest plugins/worktree-tidy/tests`.
