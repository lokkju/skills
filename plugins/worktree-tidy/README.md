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
worktree-tidy: 41 stale worktrees (40 merged, 1 detached) in skills; 1 to look at; 8 in use.
/worktree-tidy lists them with the commands to remove them (last list: ~/.cache/…/skills-….txt).
```

Claude gets the same line, plus an instruction to leave removal to you. Nothing is printed when
nothing is stale. `/worktree-tidy` (or asking Claude about stale worktrees) runs the full report:

```
skills (/home/me/projects/skills): 52 linked worktrees
  stale detached: .claude/worktrees/app-create: detached at 25b73c4fc, unused 8 days
  stale merged: .claude/worktrees/agent-a28de… [feat/forge-tracker-blocking-links]: PR #92 merged
  stale merged: .claude/worktrees/agent-a23f3… [feat/sd-forge-hosts]: PR #57 merged; branch still on the remote
  look at: .claude/worktrees/agent-ab84d… [feat/forge-tracker-label-setup]: PR #62 merged; branch still on the remote; the checkout has commits the PR doesn't
  in use: .claude/worktrees/term-recorder [fix/term-recorder-example]: locked: claude session term-recorder (pid 3947696 start 20652447); pid 3200911 (claude) running in it; session be9a7ddd-… active 4m ago
  in use: .claude/worktrees/agent-a82bd… [feat/sd-reconcile]: subagent agent-a82bd… active 51m ago
```

followed by one shell line per stale worktree, for you to paste or run with `!`:

```
{ [ -z "$(git -C <worktree> status --porcelain --ignore-submodules=none)" ] && git -C <repo> worktree remove <worktree> && git -C <repo> branch -D <branch>; } || echo 'worktree-tidy: left <worktree> (not clean, or git refused)' >&2
git -C <repo> worktree prune
```

Every line checks that the worktree is still clean before it removes it, so running them an hour
after the report can't discard work done since. `--force` appears only for a clean worktree that
has a `.gitmodules`, because git refuses to remove those without it.

## What counts as in use

Before it judges a worktree, worktree-tidy checks whether something is using it. If anything is,
the worktree is listed as "in use" with the evidence and goes no further, whatever its PR says:

- it's the worktree the session runs in;
- it's locked (`git worktree lock`); Claude Code locks the worktrees its sessions and agents are
  in, and the lock reason is shown;
- a process other than worktree-tidy has its working directory inside it, named by pid and
  command. On Linux that comes from `/proc/*/cwd` (processes it can't read are skipped); without
  `/proc` it asks `lsof`. If neither works, the report says the check couldn't run, and the stale
  list may include a worktree something is running in;
- a Claude Code transcript for it changed within the last 24 hours (`--session-hours`, or
  `WORKTREE_TIDY_SESSION_HOURS`): a session started there
  (`~/.claude/projects/<path with / and . as ->/<session>.jsonl`) or an agent given that
  worktree (`…/subagents/agent-<id>.jsonl`, whose `.meta.json` names the worktree). It names the
  session or agent and how long ago it last wrote.

The process table and the agent transcripts are read once per scan, not once per worktree.

## What counts as stale

- **merged**: the branch's newest pull request merged, no PR from the branch is open, the
  worktree's commit is in the PR, and `git status --porcelain --ignore-submodules=none` is empty.
  When the branch is still on the GitHub remote the line says so; the removal commands delete
  only the local branch.
- **closed**: the same for a PR closed without merging, with the branch also gone from the
  remote.
- **detached**: a detached checkout that's clean, on a commit some branch or tag has, and unused
  (its HEAD, index and reflog untouched) for 7 days.
- **prunable**: git already marks it prunable because its directory is gone.

"Look at" lists what is close but not safe to call stale: a merged worktree with uncommitted
changes or commits the PR doesn't have; a closed one whose branch is still on the remote; a
detached checkout
that's dirty or on a commit no branch has; and directories under `.claude/worktrees/` or `.wt/`
that git doesn't list as worktrees. These get no commands.

It never lists a repo's main checkout or a branch with an open PR or no PR at all.

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
| `WORKTREE_TIDY_SESSION_HOURS` | `24` | a worktree whose Claude Code transcript changed this recently is in use |
| `WORKTREE_TIDY_TTL` | `600` | seconds a GitHub answer is reused |
| `WORKTREE_TIDY_OFFLINE` | unset | `1` skips GitHub; only detached and prunable worktrees can be stale |

Put them in the `env` block of your Claude Code settings. The report takes `--root`, `--days`,
`--session-hours`, `--offline`, `--refresh` (ignore the cache) and `--json` directly:

```bash
plugins/worktree-tidy/skills/worktree-tidy/scripts/worktree-tidy report --root ~/projects/codexpublicus --refresh
```

Python 3.9 or newer, git 2.36 or newer, and `gh` for the PR check. Tests:
`uv run --no-project --with pytest pytest plugins/worktree-tidy/tests`.
