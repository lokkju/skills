---
name: worktree-tidy
description: Use when the user asks which git worktrees are stale or leftover, wants to clean up worktrees after PRs merged, or after the worktree-tidy session-start note says some are stale. Lists stale worktrees (merged or closed PRs, old detached checkouts, prunable ones) across a repo or a folder of repos, with the exact commands for the user to run; it never removes anything itself.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/worktree-tidy *)
---

# worktree-tidy

`${CLAUDE_PLUGIN_ROOT}/scripts/worktree-tidy report` lists the stale worktrees in the current
repo, or in every repo one level down when the current directory isn't one, and prints the shell
lines that would remove them. It only reads: git, and one `gh api graphql` query per repo for
the PRs of the worktrees' branches, cached for ten minutes.

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/worktree-tidy" report            # this repo or workspace
"${CLAUDE_PLUGIN_ROOT}/scripts/worktree-tidy" report --refresh  # ask GitHub again now
"${CLAUDE_PLUGIN_ROOT}/scripts/worktree-tidy" report --root ~/projects/a --root ~/projects/b
```

`--offline` skips GitHub (only detached and prunable worktrees can then be stale), `--days N`
changes how long a detached checkout has to sit unused (default 7), `--session-hours N` how
recently a Claude Code transcript for a worktree must have changed for it to count as in use
(default 24), and `--json` prints the same report as data.

## What to tell the user

Show the stale, "look at" and "in use" lists grouped by repo, then the commands in one code
block, as printed. Say what each group means when it isn't obvious:

- **in use**: something is using it, so it wasn't judged at all: it's this session's worktree,
  it's locked, a process has its working directory there (named by pid and command), or a Claude
  Code session or agent transcript for it changed within the window (named, with how long ago).
- **merged**: the branch's newest PR merged, no PR from it is open, the checkout's commit is in
  the PR, and the worktree is clean. "branch still on the remote" means the remote branch
  survives; the commands delete only the local one.
- **closed**: the same for a PR closed unmerged, with the branch gone from the remote.
- **detached**: a clean detached checkout on a commit a branch or tag has, unused for the
  configured number of days.
- **prunable**: git already knows the directory is gone; `git worktree prune` clears it.
- **look at**: close to stale but not safe to call it: uncommitted changes, commits the PR
  doesn't have, a closed PR whose branch is still on the remote, a detached commit no branch has,
  or a directory under `.claude/worktrees/` or `.wt/` that git doesn't list.

A "couldn't check PRs" line means gh failed for that repo; its branch worktrees weren't judged
either way, never treated as merged. A "couldn't look for processes" line means neither `/proc`
nor `lsof` worked, so tell the user a stale worktree may still have a program running in it.

## Removal is the user's call

Don't run the removal commands, and don't remove, prune or delete branches some other way. Tell
the user to run them, for example by pasting the block into their shell or prefixing each line
with `!` here. Repos with submodules need `git worktree remove --force`, which agents aren't
allowed to run; the printed `--force` lines first check that the worktree is clean, and every
line re-checks cleanliness before it removes anything, so running them later than the report is
safe. If the user wants only some removed, give them just those lines.
