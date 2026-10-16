// Find the lines of a reply that hand work back to the user without a queue entry.
//
// A reply that ends with "left for you: merge #12, run this on each host" and queues none of it is
// the slip the skill's rule can't catch on its own. The check is deliberately narrow: a false
// alarm on an ordinary explanation costs more than a missed hand-off, so it looks only for phrases
// that defer work to the user, skips anything conditional, negated, quoted or in code, and treats a
// line as queued when it names a label or matches an item already in the queue.

import type { Item } from '../types'

// A heading or lead-in line that introduces a list of what's left for the user: the phrase, then
// at most a few words ("Left for you (both repos)").
const HEADING =
  /^(?:(?:still |what's )?left (?:for|to) you|(?:still )?(?:needs?|waiting on|blocked on) (?:from )?you|for you to (?:do|handle|run|merge|review|decide)|your (?:turn|call|move|to-?dos?|action items?)|(?:action items?|next steps?|to-?dos?|manual steps?|steps?) (?:for|left for|only) you(?: can do| to do)?|what (?:you(?:'ll| will)?|still) need(?:s)? (?:to do|from you)|over to you|you(?:'ll| will)? need to(?: do)?|only you can do|on your (?:end|side))\b[^.!?]{0,30}$/i

// A phrase anywhere in a line that hands a step to the user.
const INLINE: readonly RegExp[] = [
  /\b(?:left|leaving (?:it|this|that|these|those|them)) (?:for|to) you\b/i,
  /\bover to you\b/i,
  /\bonly you can\b/i,
  /\byour call\b/i,
  /\bfor you to (?:merge|run|approve|review|decide|deploy|push|restart|rotate|check|confirm|sign|install|enable|provision)\b/i,
  /\byou(?:'ll| will)? (?:need|have) to (?:merge|run|approve|deploy|push|restart|rotate|log ?in|sign ?in|re-?auth\w*|install|enable|grant|provision|upload|create|add|set|configure|decide|choose|pick|confirm|review|update|apply|reboot|redeploy)\b/i,
  /\bplease (?:merge|approve|run|restart|push|deploy|rotate|log ?in|sign ?in|install|enable|grant|provision|upload)\b/i,
  /\brun (?:this|these|it|that|the (?:following|commands?|script)(?: below)?) on (?:each|every|all|your|both)\b/i,
]

// The phrase is hedged or negated: "if you need to roll back", "you won't need to", "no longer".
const HEDGE = /\b(?:if|unless|in case|should|whenever|until|before|after)\b[^.;:]*$|(?:\b\w+n['’]t|\bno longer|\bnever|\bnot)\s+(?:\w+\s+){0,2}$/i
const LABEL = /\b[DA]\d+\b/
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+/
const FENCE = /^\s*(?:```|~~~)/
const REF = /(?:[\w.-]+\/[\w.-]+)?[#!]\d+|https?:\/\/[^\s)\]>]+/g
const STOP = new Set(
  'that this with from have your them they then than when what will would could should there their these those about into once each also just only still need needs here been were which while after before being does done make sure'.split(' '),
)

const words = (text: string): string[] =>
  [...new Set((text.toLowerCase().match(/[a-z][a-z0-9-]{3,}/g) ?? []).filter(word => !STOP.has(word)))]
const refs = (text: string): string[] => (text.match(REF) ?? []).map(ref => ref.replace(/[.,;:]+$/, '').toLowerCase())

/** The line with list markers, emphasis, heading marks and a trailing colon taken off. */
function bare(line: string): string {
  return line
    .replace(LIST_ITEM, '')
    .replace(/^#{1,6}\s+/, '')
    .replace(/[*_`]/g, '')
    .replace(/\s*:\s*$/, '')
    .trim()
}

/** Whether the phrase at `index` in `line` is hedged or negated in its own sentence. */
function hedged(line: string, index: number): boolean {
  const before = line.slice(0, index)
  const sentence = before.slice(Math.max(before.lastIndexOf('. '), before.lastIndexOf('? '), before.lastIndexOf('! ')) + 1)
  return HEDGE.test(sentence)
}

/** Whether a sentence holding the phrase is a question, which asks rather than hands off. */
function asked(line: string, index: number): boolean {
  const rest = line.slice(index)
  const end = rest.search(/[.!?](?:\s|$)/)
  return end >= 0 && rest[end] === '?'
}

function handsOff(line: string): boolean {
  for (const pattern of INLINE) {
    const found = pattern.exec(line)
    if (found && !hedged(line, found.index) && !asked(line, found.index)) return true
  }
  return false
}

/**
 * Whether an item in the queue already covers the line: one shares a reference (`#12`, `!4`, a
 * URL) with it, or at least half its words, and two or more.
 */
function covered(line: string, queue: readonly Item[]): boolean {
  const lineRefs = refs(line)
  const lineWords = words(line)
  return queue.some(item => {
    const text = `${item.body}\n${item.description}`
    const itemRefs = refs(text)
    if (lineRefs.some(ref => itemRefs.includes(ref))) return true
    const itemWords = new Set(words(text))
    const shared = lineWords.filter(word => itemWords.has(word)).length
    return shared >= 2 && shared * 2 >= lineWords.length
  })
}

/**
 * The lines of `reply` that hand work to the user with no label and no matching item in `queue`:
 * the items listed under a hand-off heading ("Left for you:"), and lines that hand a step over
 * themselves ("You'll need to merge #12"). Code blocks and quotes are skipped. At most `limit`.
 */
export function handoffs(reply: string, queue: readonly Item[], limit = 5): string[] {
  const found: string[] = []
  let fenced = false
  let section = false
  for (const raw of (reply ?? '').split('\n')) {
    if (FENCE.test(raw)) {
      fenced = !fenced
      continue
    }
    if (fenced || /^\s*>/.test(raw)) continue
    const line = raw.trim()
    if (!line) continue
    const text = bare(line)
    const isItem = LIST_ITEM.test(raw)
    // A section runs from its heading through the list under it, blank lines and indented
    // continuations included, and ends at the first unindented line that isn't a list item.
    if (section && !isItem && !/^\s/.test(raw)) section = false
    if (!isItem && text.length <= 80 && HEADING.test(text) && (/:\s*$/.test(line) || /^#{1,6}\s/.test(line) || /^\*\*.*\*\*:?$/.test(line))) {
      section = true
      continue
    }
    const candidate = (section && isItem) || handsOff(text)
    if (!candidate || LABEL.test(line) || covered(text, queue)) continue
    found.push(text)
    if (found.length >= limit) break
  }
  return found
}

/** What the model is told on its next turn about the hand-offs its last reply left unqueued. */
export function nudge(lines: readonly string[]): string {
  return [
    'session-decisions: your last reply handed these to the user without a queue entry:',
    ...lines.map(line => `- ${line}`),
    'Anything still waiting on the user (a merge, a step on their hosts, a decision) goes in the decision queue, even when you could do it if asked: TaskCreate (a deferred tool; load it with ToolSearch first), or decision_add only when TaskCreate is not in this session. Ignore a line that is not a hand-off, or one the user has already dealt with.',
  ].join('\n')
}
