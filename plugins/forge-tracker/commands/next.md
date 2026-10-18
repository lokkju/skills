---
description: What to work on next on this repo's forge - your queue, agent lanes that can start now, and what's blocked (read-only)
argument-hint: "[--rerank] [--refresh] [--json]"
---

Run these two commands with the Bash tool, from the repo root, and wait for both:

```bash
git fetch --quiet origin
"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next $ARGUMENTS
```

`/next` recommends; it never acts. Don't claim, assign, label, comment on or branch for any item
because of this report unless the user asks. The writes it may lead to are the local config file
and, on a second yes, the missing labels, as set out under setup below.

## When the report has a `setup:` line

That line means `.claude/forge-tracker.local.md` is missing, leaves labels unset, or sets label
roles to `~` (no such label on the forge yet), so the report can't see what those labels would
mark. With no needs-human or decision label configured, open
issues that aren't blocked come out `unclassified` rather than agent-ready, and an empty queue
means no label could put anything there, not that nothing needs the user; say so. Handle setup
before ranking a slate:

- If the line says `Setup was declined`, or you are running this for your own purposes, don't
  offer anything; pass the line on once, as it is.
- Otherwise offer to set it up, once. Run
  `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels suggest`. It only reads the repo's labels
  and prints a proposed config (labels, tiers, `ruling_pattern`), where `~` records that the repo
  has no such label, plus the labels it would create for each role no label matched or the
  config sets to `~`. Show the user the proposal and ask whether to write it, as is or with
  their corrections. When it says the file `already sets every key this proposes; nothing to
  add`, there's no config question; go straight to the labels question below.
- **Yes:** write `.claude/forge-tracker.local.md` with the proposed content and the user's
  corrections. When the file already exists, add only the keys the suggestion printed and keep
  everything else in it. Don't commit it unless the user asks (committing it shares the label
  names, and whether to is the repo's call).
- **Labels that don't exist yet:** whenever the suggestion lists any (`Not on the forge ...`),
  whether or not the config file already exists, ask a second, separate question: whether to
  create them on the forge, naming each label, its color and description,
  and saying that it goes through the repo's write-wrapper when one is set. The yes to the config
  isn't a yes to this. Only on an explicit yes, run
  `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels create <names>` with exactly the names
  the user approved (a default name such as `needs-human`, or `role=name`, such as
  `agent=ready-for-agent`, for a name of their own). It refuses, creating nothing, a name the
  repo already has in any case or a role the config already maps. Then put each created name in
  the file in place of its `~`, as its output lists them. Show any `REFUSED` or `FAILED` line
  as it is and leave that role at `~`. On a no, or for labels the user didn't approve, give
  them the `gh label create` commands from the suggestion and leave the `~` in place. Never
  create labels yourself any other way, even through `gh`; those commands are the user's.
- **No:** run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" labels decline`. It writes one file
  in the local cache (never the repo or the forge), so this command won't offer setup again
  here; the report's line keeps saying what's unset, and a line about roles at `~` goes quiet.
  Tell the user they can ask for it any time. Use this for a no to the labels question too, when
  the user wants those roles left at `~`.

After writing the file, run the report command again from the top: the config is part of the
facts' fingerprint, so the classification and any slate start over.

## When the report ends with `slate: needed`

The facts changed since the last ranking (or there is none, or the user passed `--rerank`), so
rank them before presenting anything:

1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next facts --json`. Each item has its class
   (`person-only`, `agent-ready`, `blocked`, `unknown`, `unclassified`), tier, due date, labels,
   owners, blockers, what it unblocks, what has it in flight, and its body. `unclassified` means
   the repo configures no needs-human or decision label (`labelled: false`), so no label could
   say the item needs a person, and the CLI doesn't claim it's agent-ready. An `agent-ready` item
   with `untagged: true` is agent-ready only because no label said otherwise (it has neither a
   person-only label nor the agent label); that's the default, not a judgement.
2. Build the slate as JSON:

   ```json
   {
     "queue": ["#10", "#15"],
     "lanes": [
       {"area": "plugins/forge-tracker/forges", "items": ["#21", "#30"],
        "why": "both change the GitHub adapter"}
     ],
     "overrules": {"#40": "design work an agent can build from the spec"},
     "person_only": {"#15": "physical work: repaste the GPU"},
     "note": "optional, one line",
     "recommend": {
       "merge": [{"ref": "#33", "why": "release PR, CI green: the cheapest progress"}],
       "you": [{"ref": "#10", "why": "unblocks #22, #23 and #24; nothing else queued unblocks agents"},
               {"ref": "#15", "why": "the hardware gates #20"}],
       "agent_cap": 3,
       "start": [{"lane": "A", "why": "P1 bug, design ruled, due Friday"}],
       "wait": [{"lane": "B", "why": "touches files open PR #41 is changing; wait for it to merge"}],
       "not_yet": [{"ref": "#19", "why": "research with no design yet"}]
     }
   }
   ```

   - **queue**: the `person-only` items in the order the user should take them. Start from the
     facts' order (most agent-ready items unblocked first) and move items only for a reason you
     can state (a due date, a tier).
   - **lanes**: the `agent-ready` items (and the `unclassified` ones you judge agent work), by
     tier and due date, grouped so that no two lanes are
     likely to touch the same files. Judge that from titles, bodies and labels; `area` names the
     files or part of the repo the lane touches. An item with something in flight stays in its
     lane; the report marks it. Items you leave out are listed as not in a lane.
   - **overrules**: an item that's person-only only by a marker word (`marker: legal`, say), never
     by a label, may go in a lane when you give the reason here.
   - **person_only** (for `unclassified` items and `untagged` agent-ready ones): read each such
     item's title and body when ranking and decide where it goes. Put it in the queue, with a
     one-line reason here, when its text says the user has to act: a decision (a question in the
     title, options with no ruling), physical work, a device or a test window only they have,
     research that's done and waits on their decision, waiting on an external event or another
     repo, or explicitly parked. Put it in a lane when an agent can do it from what's written.
     An unclassified item you can't place, leave out; the report lists it as not placed. Never
     put a labelled item here: a person-only label already put it in the queue, an item with the
     agent label stays in the lanes, and save-slate refuses both. When you move an untagged item
     into the queue in a repo with labels, suggest to the user that they tag it (the needs-human
     or decision label) so the next run doesn't depend on your judgement; don't tag it yourself.
   - **recommend**: the closing pick, written from the facts once the queue and lanes are set.
     Judge impact: what each item unblocks, its tier and due date, bug before feature, how
     settled its design is (a ruled spec against open questions), how close it is to merged work
     that's still fresh, and the risk of conflicting with PRs in flight. Every entry needs a
     one-line `why`.
     - `merge`: open PRs ready to merge, bot PRs (Dependabot, a release PR) included, as the
       first step. Ready means not a draft, `conflicts: no`, CI `success`, and review `approved`
       or none required. `conflicts: unknown` is never ready; save-slate refuses it.
     - `you`: one pick from your queue for the user, then the next one or two only when they
       matter.
     - `start` and `wait`: every lane, by its letter, either starts now or waits, each with its
       reason. Set `agent_cap` to the number of agents this session may run at once (from the
       user's instructions; leave it out when none is set) and start no more lanes than that.
     - `not_yet`: what you wouldn't do yet, with the reason: umbrella issues, items waiting on a
       decision, drift `/groom` should fix first, PRs not ready. An item here can't also be
       picked, started or merged.
3. Save it: pipe the JSON to `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next save-slate`. A
   refusal (exit 2) names every problem; fix them and save again. It writes only to the local
   cache, never to the forge.
4. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" next` again and present that report.

A later `/next` shows the saved slate, with its age, until the facts move.

## Presenting the report

1. **Your queue** first, numbered, keeping each item's number, why it's person-only (the label or
   marker) and what it unblocks. An item marked `judged person-only: <reason>` is there by your
   judgement of its text, not by a label; say so, with the reason. When the report opens with an
   `Unlabelled:` line, say once that the queue and lanes are your reading of the issues because
   the repo has no person-only label, and pass on any `not placed` items.
   When you're presenting a report with no slate (you ran it for your own purposes, say), items
   marked `untagged` under Agent-ready are agent-ready by default only; don't present them as
   settled agent work.
2. **Agent lanes** next. Say plainly that the grouping is your judgement, not a measurement.
   Keep each item's in-flight marks (an open PR with its draft, conflict and CI state, or a pushed
   branch) and never call a lane with `conflicts: unknown` ready to merge.
3. **Blocked**: each item and what it waits on. `no blocking links recorded` means the forge
   records none; an issue whose text says it waits on something isn't counted here.
4. **Open PRs with no open issue**: each with its draft, conflict, CI and review state. These are
   in flight too; never drop them, and never call one with `conflicts: unknown` ready to merge.
5. **Flags** last: merged-but-open issues, links to closed blockers and blocked labels with
   nothing linked (all of which `/groom` fixes), blocking cycles, and pushed branches with no PR.
6. State every `FAILED (...)` line plainly, with its reason, in the section it belongs to. A
   failed read is not "nothing": never summarize it as empty or clear. When blocking links
   failed, the items listed as not classified may be blocked; don't call them agent-ready.
7. **Recommended next** closes the report: merge first, the user's pick, the lanes to start and
   the ones that wait, and what not to do yet, each with its reason. Say plainly that it's your
   judgement from the facts. It's a recommendation, not an action: ask before starting a lane,
   merging a PR or touching any item it names.

An assignee is never a reason an item is the user's or an agent's: agents share the user's
account. Show assignees as owners.

Exit status 1 means at least one read failed; the report still printed, nothing was cached for
reuse, and no slate is shown or saved until a run reads everything. Exit status 2 is a setup
problem (no origin remote, a forge it can't read yet, a bad `.claude/forge-tracker.local.md`, or
`gh` not logged in): show the message and stop.

## In a workspace folder

When the start directory isn't a git repo but holds several, `git fetch` fails there and the
report command exits 2 with `... isn't a git repo; it's a workspace of N (a, b, c)`. That isn't a
setup problem to stop at: `/next` ranks one repo at a time, because each slate belongs to one
repo's facts. Ask the user which of the listed repos to look at, or take the one the conversation
is about, and run both commands for it with `git -C <dir> fetch --quiet origin` and
`"${CLAUDE_PLUGIN_ROOT}/scripts/forge-tracker" --repo <dir> next $ARGUMENTS`. Every later command
for that repo (`next facts`, `next save-slate`, `labels suggest`) takes the same `--repo <dir>`.
For what changed across all of them, `/catchup` reads the whole workspace in one report.
