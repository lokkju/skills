import { describe, expect, test } from 'claude-code/testing'

import { findings, handoffs, nudge, parseIgnore, unknownLabels, wideCommands, wideNudge } from '../hooks/handoff'
import type { Item } from '../types'

const item = (over: Partial<Item>): Item => ({
  id: '1',
  source: 'tasks',
  label: 'A1',
  kind: 'ACTION',
  body: 'merge #12 [#12]',
  status: 'pending',
  description: '',
  ...over,
})

describe('hand-offs without labels', () => {
  test('the items under a hand-off heading', () => {
    const reply = [
      'Both fixes are pushed and CI is green.',
      '',
      'Left for you:',
      '- Merge #41 and #42',
      '- Run `mise run deploy` on each host',
      '',
      'That covers the release.',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual(['Merge #41 and #42', 'Run mise run deploy on each host'])
  })

  test('headings in their other forms', () => {
    expect(handoffs('**Next steps for you:**\n1. Approve the release PR', [])).toEqual(['Approve the release PR'])
    expect(handoffs('## What you need to do\n\n- rotate the staging key\n- restart nginx', [])).toEqual([
      'rotate the staging key',
      'restart nginx',
    ])
    expect(handoffs("You'll need to:\n* log in to the registry", [])).toEqual(['log in to the registry'])
  })

  test('a line that hands a step over by itself', () => {
    expect(handoffs("The branch is ready. You'll need to merge it once CI passes.", [])).toHaveLength(1)
    expect(handoffs('I can’t reach prod, so this one is left for you.', [])).toHaveLength(1)
    expect(handoffs('Whether to drop the v1 endpoint is your call.', [])).toHaveLength(1)
    expect(handoffs('Run these on each host:', [])).toHaveLength(1)
    expect(handoffs('Please merge #12 when you have a moment.', [])).toEqual(['Please merge #12 when you have a moment.'])
  })

  test('at most five lines come back', () => {
    const reply = ['Left for you:', ...[1, 2, 3, 4, 5, 6, 7].map(n => `- step ${n}`)].join('\n')
    expect(handoffs(reply, [])).toHaveLength(5)
  })
})

describe('user-only steps described in prose', () => {
  // The shape of every queue miss in a 54-item session: an ACTION that blocks only the user,
  // handed over in prose with no list and no "left for you".
  test('a blocker, a step the assistant cannot take, an offer waiting on the word', () => {
    const lines = [
      'Putting the client secret in Vault is the only thing blocking the build restart.',
      'I cannot write to Vault for you.',
      "I can't access the staging cluster, so the token rotation has to happen on your side.",
      "I don't have access to the registry account.",
      'Say the word and I will start the push.',
      'Nine commits are ready; approve the SSH key prompt and they go out.',
      'Approve the prompt.',
      'My recommendation: put the client secret in Vault under the service path, then I restart the build.',
      'The push needs your SSH key approval.',
      'The index build is waiting on you.',
      "You'll need to answer the passphrase prompt in Bitwarden.",
    ]
    for (const line of lines) expect(handoffs(line, [])).toEqual([line])
  })

  test('the same steps stay quiet once queued, hedged or done by the assistant', () => {
    const queue = [item({ body: 'put the client secret in Vault so the build can restart [no link]' })]
    expect(handoffs('Putting the client secret in Vault is the only thing blocking the build restart.', queue)).toEqual([])
    expect(handoffs("Once that's merged I'll approve the deploy request myself.", [])).toEqual([])
    expect(handoffs("I can't reproduce the failure locally, so I added logging.", [])).toEqual([])
    expect(handoffs('If I cannot write to Vault for you, the build waits.', [])).toEqual([])
    expect(handoffs('Say the word?', [])).toEqual([])
  })
})

describe('labels the queue does not hold', () => {
  const known = ['D1', 'A1', 'A2']

  test('a line naming a label that was never created is flagged, whatever its wording', () => {
    const reply = [
      'Done with the analysis.',
      "(A24 is in the queue for that so it doesn't become the next thing I only mention.)",
      "I've queued it as D33 with that scope proposal, to run after the 25th.",
    ].join('\n')
    expect(handoffs(reply, [], { known })).toEqual([
      "(A24 is in the queue for that so it doesn't become the next thing I only mention.)",
      "I've queued it as D33 with that scope proposal, to run after the 25th.",
    ])
    expect(unknownLabels(reply, known)).toEqual(['A24', 'D33'])
  })

  test('a known label still covers its line, and look-alikes, code and quotes are not labels', () => {
    const reply = [
      'Left for you:',
      '- A2: rotate the key',
      'D1 is settled and A1 is done.',
      'The D3.js chart renders fine.',
      'The skill says to write `D3` in your message, and the toast read "A9 added".',
    ].join('\n')
    expect(handoffs(reply, [], { known })).toEqual([])
    expect(unknownLabels(reply, known)).toEqual([])
  })

  test('a label in a hand-off line covers it only when the queue holds it', () => {
    expect(handoffs("You'll need to merge #12 (A1).", [], { known })).toEqual([])
    expect(handoffs("You'll need to merge #12 (A7).", [], { known })).toEqual(["You'll need to merge #12 (A7)."])
    expect(unknownLabels("You'll need to merge #12 (A7).", known)).toEqual([])
  })

  test('a label quoted while describing an issue, a PR, a link or another session is not flagged', () => {
    const reply = [
      'Tests: the agent added tests for the exact D123 shape from the issue.',
      '- [skills#129](https://github.com/lokkju/skills/issues/129): the wrap-up nudge misread an answer; D123 sent a nested note about four-digit years instead of the recommendation.',
      'Fixed in #140, where its A9 never reached the queue.',
      'The other session had D14 open on the same question.',
    ].join('\n')
    expect(handoffs(reply, [], { known })).toEqual([])
    expect(unknownLabels(reply, known)).toEqual([])
  })

  test('a label far above the session’s own reads as another queue’s', () => {
    const many = Array.from({ length: 52 }, (_, i) => `D${i + 1}`)
    expect(unknownLabels('D123 sent a nested note about years.', many)).toEqual([])
    expect(unknownLabels('D60 covers the rename.', many)).toEqual(['D60'])
  })

  test('a label the session writes as its own is still flagged when unqueued', () => {
    expect(handoffs('D7: merge X', [], { known })).toEqual(['D7: merge X'])
    expect(unknownLabels('D7: merge X', known)).toEqual(['D7'])
    // Leading the line, it is the session's even beside a reference.
    expect(unknownLabels('- A5: merge [#12](https://github.com/o/r/pull/12)', known)).toEqual(['A5'])
    // A reference in another sentence of the line doesn't excuse it.
    expect(unknownLabels('Merged #12. I queued the rename as D8.', known)).toEqual(['D8'])
  })
})

describe('hand-offs that are queued', () => {
  test('a line that names a label', () => {
    const reply = 'Left for you:\n- A1: merge #41\n- D2 is the cutover date\n- (A3) run the backfill on each host'
    expect(handoffs(reply, [])).toEqual([])
    expect(handoffs("You'll need to merge #12 (A1).", [])).toEqual([])
  })

  test('a line an item in the queue already covers, by reference or by its words', () => {
    const queue = [
      item({ body: 'merge #41 [#41]' }),
      item({ id: '2', label: 'A2', body: 'rotate the staging deploy key [no link]' }),
      item({ id: '3', label: 'A3', body: 'review the docs [https://example.com/pr/9]' }),
    ]
    const reply = [
      'Left for you:',
      '- Merge #41',
      '- Rotate the staging deploy key',
      '- Look over https://example.com/pr/9',
      '- Restart the worker on box3',
    ].join('\n')
    expect(handoffs(reply, queue)).toEqual(['Restart the worker on box3'])
  })
})

describe('prose that only resembles a hand-off', () => {
  test('conditional, negated and questioning phrasing', () => {
    expect(handoffs("If you need to roll back, run `git revert HEAD`.", [])).toEqual([])
    expect(handoffs("You won't need to restart anything; the config reloads itself.", [])).toEqual([])
    expect(handoffs("You don't need to merge anything by hand.", [])).toEqual([])
    expect(handoffs('Should you need to rotate the key later, the script is in bin/.', [])).toEqual([])
    expect(handoffs('Do you want me to merge it, or will you need to review it first?', [])).toEqual([])
    expect(handoffs("It isn't your call alone; the team decides.", [])).toEqual([])
    expect(handoffs("There's nothing you have to configure.", [])).toEqual([])
    expect(handoffs("That's what's waiting on you, and everything it's waiting on you for.", [])).toEqual([])
  })

  test('code blocks and quotes', () => {
    const reply = [
      'The README now says:',
      '',
      "> You'll need to run `make install` on each host.",
      '',
      '```',
      'Left for you:',
      "- you'll need to merge this",
      '```',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual([])
  })

  test("the assistant's own plan, explanations and summaries", () => {
    const reply = [
      'Done. I merged #41, ran the migrations and restarted the worker.',
      '',
      'Next steps:',
      '- I will write the changelog entry',
      '- then tag the release',
      '',
      'You can run `mise run test` to check it yourself, and the logs are on your side of the VPN.',
      'To deploy, the workflow runs `kubectl apply` for you.',
    ].join('\n')
    expect(handoffs(reply, [])).toEqual([])
  })

  test('a phrase inside quotes, or a line about the check itself', () => {
    // The two lines from issue 99.
    const description = 'It’ll lay out two or three options with a recommendation and the questions only you can answer.'
    expect(handoffs(description, [])).toEqual([])
    expect(
      handoffs(
        `Hand-off warning false positive: "${description}" That line described the agent's planned output, not a step for you.`,
        [],
      ),
    ).toEqual([])
    expect(
      handoffs(
        "Hand-off warning false positive: the line about the questions only you can answer described the agent's planned output, not a step for you.",
        [],
      ),
    ).toEqual([])
    expect(handoffs('The README tells the reader "you\'ll need to merge it once CI passes".', [])).toEqual([])
    expect(handoffs('“Left for you” and ‘your call’ are the phrases it looks for.', [])).toEqual([])
    expect(handoffs("The phrase `you'll need to merge` is the one it looks for.", [])).toEqual([])
  })

  test('what another agent, a document or a tool will produce', () => {
    expect(handoffs('The report lists the steps you’ll need to run on each host.', [])).toEqual([])
    expect(handoffs('The research agent will flag anything only you can decide.', [])).toEqual([])
    expect(handoffs("It'll come back with a plan and the calls only you can make.", [])).toEqual([])
    // A third line from issue 99's session: a feature's output, not a step for the user.
    expect(
      handoffs(
        'C, [#103](https://github.com/lokkju/skills/issues/103): a session-start hook that reports stale git worktrees, likely as a small new plugin. It prints the cleanup commands for you to run rather than deleting anything, keeps within a time budget, and does a read-only dry run over your skills and codexpublicus repos.',
        [],
      ),
    ).toEqual([])
    // A future that waits on the user still counts.
    expect(handoffs("It'll need your approval before the deploy runs.", [])).toHaveLength(1)
    expect(handoffs("That'll be your call.", [])).toHaveLength(1)
  })

  test('"only you can" counts for a step, not for questions only you can answer', () => {
    expect(handoffs('Only you can approve the merge.', [])).toHaveLength(1)
    expect(handoffs('Rotating the root key is something only you can do.', [])).toHaveLength(1)
    expect(handoffs('I listed the questions only you can answer.', [])).toEqual([])
  })

  test('a command in a code span still hands a step over', () => {
    expect(handoffs("You'll need to run `mise run deploy --env prod --all-hosts` yourself.", [])).toHaveLength(1)
  })

  test('a hand-off heading word in an ordinary sentence opens no section', () => {
    expect(handoffs('Nothing is left for the cleanup job.\n- it runs nightly', [])).toEqual([])
    expect(handoffs('For you to compare, the two plans are:\n- A: one host\n- B: two hosts', [])).toEqual([])
  })
})

test('the nudge quotes the lines and says to ignore a false match', () => {
  const text = nudge(['Merge #41'])
  expect(text).toContain('- Merge #41')
  expect(text).toContain('TaskCreate')
  expect(text).toContain('Ignore a line that is not a hand-off')
  expect(text).not.toContain('never created')
})

test('the nudge says a named label was never created, and to make the tool call first', () => {
  const text = nudge(['(A24 is in the queue)'], ['A24'])
  expect(text).toContain("Your reply named A24, but no such item is in this session's queue")
  expect(text).toContain("If it is one of this session's items, it was never created: create it now")
  expect(text).toContain('make the tool call before writing the sentence that names a label')
  expect(text).toContain('Ignore a label you quoted from an issue, a PR, a document or another session')
})

describe('command lines too wide to copy out of tmux', () => {
  const long = 'sudo systemctl stop heartbeat.timer && sudo rm /etc/systemd/system/heartbeat.* && sudo systemctl daemon-reload'

  test('a long line in a shell block, or a command in an unlabelled block, is caught', () => {
    expect(long.length).toBeGreaterThan(100)
    expect(wideCommands(['Run this on each host:', '```bash', long, '```'].join('\n'))).toEqual([long.length])
    expect(wideCommands(['```', long, '```'].join('\n'))).toEqual([long.length])
    expect(wideCommands(['```console', `$ ${long}`, '```'].join('\n'))).toEqual([long.length + 2])
  })

  test('short lines, continuations, prose and other languages pass', () => {
    const split = ['```bash', 'sudo systemctl stop heartbeat.timer \\', '  && sudo rm /etc/systemd/system/heartbeat.* \\', '  && sudo systemctl daemon-reload', '```']
    expect(wideCommands(split.join('\n'))).toEqual([])
    expect(wideCommands(`A paragraph of prose that runs well past a hundred columns: ${long}`)).toEqual([])
    expect(wideCommands(['```json', `{"command": "${long}"}`, '```'].join('\n'))).toEqual([])
    const log = 'Oct 26 10:14:03 host1 systemd[1]: heartbeat.service: Deactivated successfully after a long run of checks today.'
    expect(log.length).toBeGreaterThan(100)
    expect(wideCommands(['```', log, '```'].join('\n'))).toEqual([])
  })

  test('the reminder names the width and says to hand over a script or split the lines', () => {
    const text = wideNudge([112, 140])
    expect(text).toContain('2 shell command lines over 100 columns')
    expect(text).toContain('the widest 140')
    expect(text).toContain('a script with one short line that runs it')
    expect(text).toContain('`\\` continuations')
  })
})

// The false positives a 2026-10-22 to 2026-11-05 measurement found (lokkju/skills#163), from the
// lines it flagged, and the true positives the fixes must keep.
describe('false positives from the measurement', () => {
  const open = [item({ label: 'A9', body: 'something unrelated [no link]' })]

  test('Cloudflare D1 next to another cloud resource is not a label', () => {
    for (const line of [
      'Retire the heartbeat once a timed test passes: the Worker, its D1, the host timer and the CronJob.',
      "- each Worker repo's CI pushes its bundle and a list of its bindings (D1, KV, Durable Objects, cron, vars) to zot;",
      '- the qusme Worker on app.qus.me, its D1 database and its deploy token;',
      '| Storage | R2: a public bucket for items.json, a private one for state | One D1 database for every site, R2 for images |',
      'Tests: the same 34 tests pass on SQLite and on D1.',
    ]) {
      expect(unknownLabels(line, ['A1'])).toEqual([])
      expect(handoffs(line, [], { known: ['A1'] })).toEqual([])
    }
  })

  test('a label quoted in a table cell is not claimed by its repeat in the same row', () => {
    expect(handoffs('| "D1" meaning Cloudflare D1 | 25 |', [], { known: ['A1'] })).toEqual([])
    expect(handoffs('| "D7" quoted, then D7 again | 2 |', [], { known: ['A1'] })).toEqual([])
  })

  test('labels the ignore_labels option lists are never claimed', () => {
    const line = '| 3 | The D1 tenant store; config changes apply within 30 seconds, without a redeploy |'
    expect(unknownLabels(line, ['A1'])).toEqual(['D1'])
    expect(unknownLabels(line, ['A1'], { ignore: ['D1'] })).toEqual([])
    expect(handoffs(line, [], { known: ['A1'], ignore: ['D1'] })).toEqual([])
    expect(parseIgnore('D1, d2 A3; nope')).toEqual(['D1', 'D2', 'A3'])
    expect(parseIgnore(undefined)).toEqual([])
  })

  test('a label leading its line, or one named outside a cloud line, still flags', () => {
    expect(handoffs('D7: merge it', [], { known: ['D1'] })).toEqual(['D7: merge it'])
    expect(handoffs('D7: create the R2 bucket for the site', [], { known: ['D1'] })).toEqual(['D7: create the R2 bucket for the site'])
    expect(unknownLabels("I've queued it as D33 with that scope proposal.", ['D1'])).toEqual(['D33'])
  })

  test('a negated status line is not a hand-off', () => {
    for (const line of [
      'Nothing is waiting on you; the decision queue is empty.',
      'Nothing is waiting on you, and the standing backlog line reads standing: none.',
      'V2 is complete and reviewed. E14 follows. Nothing is waiting on you.',
      'No action is waiting on you.',
    ]) {
      expect(handoffs(line, [], { known: [] })).toEqual([])
    }
  })

  test('a line saying the items are queued is a status line', () => {
    const reply = [
      'Two decisions only you can make are in the queue:',
      '- **D17**: whether to fetch the PDFs through `/api/uploads?p=`. I recommend yes.',
      '- **D18**: which document types to collect. I recommend all nine types.',
    ].join('\n')
    expect(handoffs(reply, [], { known: ['D17', 'D18'] })).toEqual([])
    expect(handoffs('Both are already in the decision queue, and only you can approve them.', [], { known: [] })).toEqual([])
    // Negated, it says the opposite.
    expect(handoffs("These aren't in the queue yet, and only you can approve them.", [], { known: [] })).toEqual([
      "These aren't in the queue yet, and only you can approve them.",
    ])
  })

  test('a count of decisions waiting is a status line while the queue holds open items', () => {
    const line = 'All three pieces are done, and three decisions are waiting on you.'
    expect(handoffs(line, open, { known: ['A9'] })).toEqual([])
    expect(handoffs(line, [], { known: [] })).toEqual([line])
  })

  test('a heading or list item whose list items all name queued labels is not a hand-off', () => {
    const merges = [
      '**Waiting on you:**',
      '- **Merges:**',
      '  - A206: HSTS, [infra#91](https://github.com/codexpublicus/infra/pull/91).',
      '  - A207: exemplar 0.6.1, [exemplar#61](https://github.com/codexpublicus/exemplar/pull/61).',
      '- **Decisions:** D164, D166, D167.',
      '- **Cleanup:** A212, the scratch directories outside the job folder.',
    ].join('\n')
    expect(handoffs(merges, [], { known: ['A206', 'A207', 'A212', 'D164', 'D166', 'D167'] })).toEqual([])
    const these = ['These are still waiting on you:', '- **A182:** raise the quota.', '- **D221:** the nine leftover worktrees.'].join('\n')
    expect(handoffs(these, [], { known: ['A182', 'D221'] })).toEqual([])
    // Items nested under a queued item belong to it.
    const nested = ['**Waiting on you:**', '- **D225:** the Plex phase. I recommend:', '  - keeping its address;', '  - CPU transcoding for now;'].join('\n')
    expect(handoffs(nested, [], { known: ['D225'] })).toEqual([])
    // A heading that names a queued label covers its list.
    expect(handoffs('**Your turn, on ml-01 (A157):**\n1. Run the render yourself.\n2. Run the postcheck.', [], { known: ['A157'] })).toEqual([])
    // A list item saying there's nothing there.
    expect(handoffs('**Waiting on you**\n- Nothing on the standing backlog.', [], { known: [] })).toEqual([])
  })

  test('a heading over an unqueued item still flags it, and so does the heading over unlabelled items', () => {
    expect(handoffs('Left for you:\n- A3: merge #12\n- rotate the key', [], { known: ['A3'] })).toEqual(['rotate the key'])
    expect(handoffs('Actions only you can do:\n- turn off Dependabot\n- run the probe', [], { known: [] })).toEqual(['Actions only you can do'])
    expect(handoffs('Actions only you can do:\n- A1: turn off Dependabot\n- run the probe', [], { known: ['A1'] })).toEqual(['Actions only you can do'])
  })

  test("labels from another session, a plan or a document are not the session's", () => {
    const known = ['D1', 'A1']
    expect(
      unknownLabels(
        "The lokkju/infra session asked whether captare's olmOCR backlog run, the GPU job your D242 ruling covers, was already planned from this session.",
        known,
      ),
    ).toEqual([])
    expect(unknownLabels("I haven't started it, because your vaglio session filed it as D9.", known)).toEqual([])
    expect(unknownLabels("captare's own docs list the full backlog run as A7.", known)).toEqual([])
    expect(unknownLabels("The full backlog run is the plan's operator action A3.", known)).toEqual([])
    expect(unknownLabels('The plan and rulings behind it are in `docs/plans/2026-10-08-pets-incus-happier.md` (D69, D77).', known)).toEqual([])
    expect(unknownLabels('The D242 the hook flagged is a lokkju/infra ruling, not an item.', known)).toEqual([])
    expect(unknownLabels('The session-decisions hook flagged D174 and A221 as missing from the queue.', known)).toEqual([])
    // This session's own, named plainly, still counts.
    expect(unknownLabels('D9 is waiting on you.', known)).toEqual(['D9'])
  })

  test('a label the previous nudge flagged is not flagged again unless the reply hands it over anew', () => {
    const flagged = { labels: ['D33'], lines: ['Merge #41 and #42'] }
    expect(handoffs('Sorry, D33 was my slip: there is no such item.', [], { known: ['D1'], flagged })).toEqual([])
    expect(handoffs("D33: you'll need to merge #12", [], { known: ['D1'], flagged })).toEqual(["D33: you'll need to merge #12"])
    expect(handoffs('Left for you:\n- Merge #41 and #42', [], { known: ['D1'], flagged })).toEqual([])
    expect(handoffs('Left for you:\n- Merge #43', [], { known: ['D1'], flagged })).toEqual(['Merge #43'])
  })

  test('the phrases the measurement missed', () => {
    expect(handoffs('The config has a placeholder owner and zone ID for you to fill in.', [], { known: [] })).toHaveLength(1)
    expect(handoffs("Who merges it: you, or that repo's session?", [], { known: [] })).toHaveLength(1)
    expect(handoffs('Three judgement calls are flagged in the PR for you to check.', [], { known: [] })).toHaveLength(1)
  })

  test('the true positives stay flagged', () => {
    for (const line of [
      '[#13](https://github.com/lokkju/edgekms/issues/13), the dedicated Cloudflare account: only you can create the account and the token.',
      "New Dependabot PRs will still arrive. To stop them, turn off Dependabot security updates in the fork's settings; that's a toggle only you can flip.",
      "Codex Publicus isn't a legal entity, so payouts go to you personally unless a fiscal host holds them. Tax treatment is your call.",
      "The vLLM load test hasn't run. vLLM checks an API key, and I don't have permission to read that key, so the agent stopped before sending any load.",
      "I haven't started the vagl.io import, because your vaglio session filed it and may be doing it itself. Say the word and I'll take it.",
    ]) {
      expect(handoffs(line, open, { known: ['A9'] })).toEqual([line])
    }
  })

  test('findings say why each line was flagged', () => {
    const found = findings("Left for you:\n- Rotate the key\n\nI've queued it as D33.\n\nOnly you can approve the merge.", [], { known: ['D1'] })
    expect(found.map(one => one.reason)).toEqual(['under a hand-off heading', 'names D33, not in the queue', 'hand-off phrase "Only you can"'])
    expect(found[1]?.labels).toEqual(['D33'])
  })
})
