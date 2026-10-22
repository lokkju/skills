import { describe, expect, test } from 'claude-code/testing'

import { handoffs, nudge, unknownLabels } from '../hooks/handoff'
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
  expect(text).toContain('Your reply named A24, but no such item is in the queue')
  expect(text).toContain('make the tool call before writing the sentence that names a label')
})
