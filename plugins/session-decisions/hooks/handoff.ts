// Find the lines of a reply that hand work back to the user without a queue entry.
//
// A reply that ends with "left for you: merge #12, run this on each host" and queues none of it is
// the slip the skill's rule can't catch on its own. The misses that matter most are ACTION items
// described in prose: a secret only the user can put in Vault, a prompt only they can approve, an
// offer to go ahead once they say so. Those never block the assistant, so nothing in its own loop
// notices them. The check looks for phrases that defer work to the user or say the assistant can't
// do a step, skips anything conditional, negated, quoted, in code, describing what some other
// agent or document will say, or talking about this check itself, and treats a line as queued
// when it names a label in the queue or matches an item already there. A label the reply names
// that isn't in the queue at all ("A24 is in the queue") is flagged on its own: it is the reply
// asserting an item the assistant never created. A label quoted while describing somewhere else
// (an issue, a PR, a link, another session's queue) isn't the session's own, so it isn't flagged.

import type { Item } from '../types'

// A heading or lead-in line that introduces a list of what's left for the user: the phrase, then
// at most a few words ("Left for you (both repos)").
const HEADING =
  /^(?:(?:still |what's )?left (?:for|to) you|(?:still )?(?:needs?|waiting on|blocked on) (?:from )?you|for you to (?:do|handle|run|merge|review|decide)|your (?:turn|call|move|to-?dos?|action items?)|(?:action items?|next steps?|to-?dos?|manual steps?|steps?) (?:for|left for|only) you(?: can do| to do)?|what (?:you(?:'ll| will)?|still) need(?:s)? (?:to do|from you)|over to you|you(?:'ll| will)? need to(?: do)?|only you can do|on your (?:end|side))\b[^.!?]{0,30}$/i

// Steps that only the user can take: access, secrets, prompts, accounts, money, hardware.
const USER_STEP =
  'merge|run|approve|deploy|push|restart|rotate|log ?in|sign ?in|re-?auth\\w*|install|enable|grant|provision|upload|create|add|set|configure|decide|choose|pick|confirm|review|update|apply|reboot|redeploy|put|store|paste|enter|answer|unlock|accept|touch|tap|press|click|share|invite|pay|buy|order|plug|call|email|contact|request|register|renew|verify|authori[sz]e|authenticate|generate|revoke|sign'

// Something a prompt, a key or an account asks the user to act on.
const PROMPTED = '(?:prompts?|dialogs?|requests?|notifications?|pop-?ups?|keys?|yubikey|passkey|2fa|mfa|otp|codes?|push(?:es)?)'
const SECRET = '(?:secrets?|tokens?|passwords?|passphrases?|credentials?|(?:api |ssh |deploy |private )?keys?)'

// Where a sentence starts, or an imperative is plainly addressed to the reader.
const IMPERATIVE = "(?:^|[.;:!]\\s+|\\b(?:please|just|then)\\s+)"

// A phrase anywhere in a line that hands a step to the user.
const INLINE: readonly RegExp[] = [
  /\b(?:left|leaving (?:it|this|that|these|those|them)) (?:for|to) you\b/i,
  /\bover to you\b/i,
  // "only you can approve the merge", but not "the questions only you can answer".
  /\bonly you can\b(?! (?:answer|know|say|tell|judge|weigh|decide|settle)\b(?:\s+(?:below|above|here|later))?\s*(?:[.,;:!?)]|$|(?:and|or|but)\b))/i,
  /\byour call\b/i,
  new RegExp(`\\bfor you to (?:${USER_STEP})\\b`, 'i'),
  new RegExp(`\\byou(?:['’]ll| will)? (?:need|have) to (?:${USER_STEP})\\b`, 'i'),
  new RegExp(`\\bplease (?:${USER_STEP})\\b`, 'i'),
  /\brun (?:this|these|it|that|the (?:following|commands?|script)(?: below)?) on (?:each|every|all|your|both)\b/i,
  // What's blocked on the user, said as a fact rather than a request.
  /\bthe (?:only|one|last) thing (?:blocking|holding up|standing between|in the way of|stopping|gating|left before)\b/i,
  // "The build is waiting on you", but not "what's waiting on you" or "everything it's waiting on
  // you for", which describe a list rather than hand anything over.
  /(?<!\bwhat)(?:\s(?:is|are)|['’](?:s|re)) (?:still |now )?(?:waiting|blocked) (?:on|for) (?:you\b(?!\s+for\b)|your (?!own\b))/i,
  /\bneeds? your (?:[\w-]+ ){0,2}(?:approval|sign-?off|credentials?|password|passphrase|token|key|login|touch|go-?ahead|ok|okay|confirmation|answer|decision|input)\b/i,
  // The assistant can't do the step, so the user has to.
  /\bI (?:can(?:no|['’])?t|cannot|can not|am unable to|have no way to)\b[^.;!?]{0,60}\bfor you\b/i,
  /\bI (?:can(?:no|['’])?t|cannot|can not|am unable to) (?:access|reach|write to|log ?in(?: ?to)?|sign ?in(?: ?to)?|ssh (?:in)?to|unlock|answer|approve)\b/i,
  /\bI (?:(?:don['’]t|do not) have|have no) (?:the )?(?:access|permissions?|credentials?|rights)\b/i,
  // An offer that waits on the user's word.
  /\b(?:say the word|give me the (?:go-?ahead|green light|word))\b/i,
  // An imperative step only the user can take: a prompt to approve, a secret to put somewhere.
  new RegExp(`${IMPERATIVE}(?:approve|answer|accept|confirm|unlock|touch|tap)\\s+(?:the |that |this |each |every |your )?(?:[\\w-]+\\s+){0,3}${PROMPTED}\\b`, 'i'),
  new RegExp(`${IMPERATIVE}(?:put|store|add|paste|set|save|write|load)\\s+(?:the |that |this |your |a |an |both )?(?:[\\w-]+\\s+){0,3}${SECRET}\\b[^.;]{0,40}\\b(?:in|into|to|under)\\b`, 'i'),
]

// The phrase is hedged or negated: "if you need to roll back", "you won't need to", "no longer".
const HEDGE = /\b(?:if|unless|in case|should|whenever|until|before|after)\b[^.;:]*$|(?:\b\w+n['’]t|\bno longer|\bnever|\bnot|\bnothing)\s+(?:\w+\s+){0,2}$/i

// A sentence about what another agent, a document or a tool will produce, which hands nothing to
// the user however it words that output: "It'll lay out ... the questions only you can answer",
// "The report lists the steps you'll need to run". A future that waits on the user ("it'll need
// your approval", "that'll be your call") still counts.
const DESCRIBED =
  /^\s*(?:it|they|this|that|the [\w-]+(?: [\w-]+){0,2})(?:(?:['’]ll| will| would|['’]d) (?!need\b|have to\b|require|wait|block|be (?:left|waiting|blocked|up to|your|on you|over))|\s+(?:lists|lays out|covers|includes|describes|explains|names|shows|asks|outlines|summari[sz]es|gives|proposes|recommends|contains|mentions|flags|prints|writes|reports|returns|produces|generates|outputs|emits|suggests|offers|drafts)\b)/i

// A line about the hand-off check itself, such as a note on a false alarm.
const ABOUT_CHECK = /\bhand-?offs? (?:check|warning|nudge|reminder|toast|alarm)|\bfalse (?:positive|alarm)|\bnot queued:/i

const LABEL = /\b[DA]\d+\b/
// A queue label as a word of its own: not "D3.js", "A4-size" or a path.
const LABELS = /(?<![\w./#-])[DA]\d{1,3}(?![\w-]|\.\w)/g
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+/
const FENCE = /^\s*(?:```|~~~)/
// Quoted and code spans: straight and curly double quotes, curly single quotes, backticks, and
// straight single quotes that open and close a span rather than an apostrophe.
const QUOTED = /"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|`[^`\n]*`|(?<![\w'])'[^'\n]+'(?![\w'])/g
const REF = /(?:[\w.-]+\/[\w.-]+)?[#!]\d+|https?:\/\/[^\s)\]>]+/g
// A sentence that says where it is quoting from: "from the issue", "in that PR", "the other session".
const ELSEWHERE =
  /\b(?:from|in|of|per) (?:the|that|this|its|their|another|the other|other) (?:[\w-]+ )?(?:issues?|PRs?|MRs?|pull requests?|merge requests?|tickets?|threads?|transcripts?|sessions?|bug reports?|comments?|commits?)\b|\b(?:another|the other|other|that|an earlier|a previous|the previous|the last) session\b/i
// A label this far above the highest of its letter the session has used reads as another queue's.
const FOREIGN_GAP = 50
// A label leading its line or bullet ("D7: merge it", "**A3** (...)"): how the session writes its own.
const LEADING = /^\s*(?:(?:[-*+]|\d+[.)])\s+)?[*_]*([DA]\d{1,3})[*_]*\s*[:(]/
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

/** The line with its quoted and code spans blanked out, so a phrase inside one doesn't count. */
function unquoted(line: string): string {
  return line.replace(QUOTED, ' … ')
}

/** Whether most of a line is quotation; a code span is a command to run, not a quotation. */
function mostlyQuoted(line: string): boolean {
  const quoted = (line.match(QUOTED) ?? []).filter(span => !span.startsWith('`')).join('').replace(/\s/g, '').length
  return quoted * 2 >= line.replace(/\s/g, '').length
}

/** The sentence of `line` that the phrase at `index` sits in, up to the phrase. */
function sentenceBefore(line: string, index: number): string {
  const before = line.slice(0, index)
  return before.slice(Math.max(before.lastIndexOf('. '), before.lastIndexOf('? '), before.lastIndexOf('! ')) + 1)
}

/** Whether the phrase at `index` in `line` is hedged or negated in its own sentence. */
function hedged(line: string, index: number): boolean {
  return HEDGE.test(sentenceBefore(line, index))
}

/** Whether the phrase's sentence describes what some other agent, document or tool will say. */
function described(line: string, index: number): boolean {
  // The verb after "will" can be the phrase itself ("it'll need your approval"), so read on past it.
  return DESCRIBED.test(sentenceBefore(line, index) + line.slice(index))
}

/** Whether a sentence holding the phrase is a question, which asks rather than hands off. */
function asked(line: string, index: number): boolean {
  const rest = line.slice(index)
  const end = rest.search(/[.!?](?:\s|$)/)
  return end >= 0 && rest[end] === '?'
}

/** Whether `line` (trimmed, its markup intact) hands a step over by itself. */
function handsOff(line: string): boolean {
  if (ABOUT_CHECK.test(line) || mostlyQuoted(line.replace(LIST_ITEM, '').replace(/[*_]/g, ''))) return false
  const text = bare(unquoted(line))
  for (const pattern of INLINE) {
    const found = pattern.exec(text)
    if (found && !hedged(text, found.index) && !asked(text, found.index) && !described(text, found.index)) return true
  }
  return false
}

/** The queue labels a line names outside its quoted and code spans. */
function labelsIn(line: string): string[] {
  return unquoted(line).match(LABELS) ?? []
}

/** Whether `label` sits far above every label of its letter in `known`, as another queue's would. */
function far(label: string, known: readonly string[]): boolean {
  const top = Math.max(0, ...known.filter(other => other[0] === label[0]).map(other => Number(other.slice(1))))
  return Number(label.slice(1)) > top + FOREIGN_GAP
}

/**
 * The labels `line` claims as the session's own: those outside its quoted and code spans, less the
 * ones quoted while describing somewhere else. A label is taken as someone else's when its bullet,
 * or its sentence in other lines, holds an issue or PR reference or a link, or says where it quotes
 * from ("from the issue"), or when it sits far above the session's labels. A label leading the line
 * ("D7: merge it") is always the session's.
 */
function claimedIn(line: string, known: readonly string[]): string[] {
  const lead = LEADING.exec(line)?.[1]
  const scopes = LIST_ITEM.test(line) ? [line] : line.split(/(?<=[.!?])\s+/)
  const claimed: string[] = []
  for (const scope of scopes) {
    const elsewhere = refs(scope).length > 0 || ELSEWHERE.test(unquoted(scope))
    for (const label of labelsIn(scope)) {
      if (label === lead || (!elsewhere && !far(label, known))) claimed.push(label)
    }
  }
  return claimed
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

/** The prose lines of a reply, with code blocks and quoted lines left out. */
function* prose(reply: string): Generator<string> {
  let fenced = false
  for (const raw of (reply ?? '').split('\n')) {
    if (FENCE.test(raw)) {
      fenced = !fenced
      continue
    }
    if (fenced || /^\s*>/.test(raw)) continue
    yield raw
  }
}

export type Options = {
  /**
   * Every label in the session's queue, settled items included. When given, a line counts as
   * queued only by a label in it, and a line naming a label that isn't is flagged. Without it,
   * any label counts.
   */
  known?: readonly string[]
  /** At most this many lines come back. */
  limit?: number
}

/**
 * The lines of `reply` that hand work to the user with no label and no matching item in `queue`:
 * the items listed under a hand-off heading ("Left for you:"), lines that hand a step over
 * themselves ("You'll need to merge #12", "I can't write to Vault for you"), and, with `known`,
 * lines naming a label that isn't in the queue. Code blocks and quotes are skipped.
 */
export function handoffs(reply: string, queue: readonly Item[], options: Options = {}): string[] {
  const { known, limit = 5 } = options
  const found: string[] = []
  let section = false
  for (const raw of prose(reply)) {
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
    if (known && claimedIn(line, known).some(label => !known.includes(label))) {
      found.push(text)
    } else {
      const candidate = (section && isItem) || handsOff(line)
      // A label quoted from elsewhere doesn't queue the line; only one the queue holds does.
      const labelled = known ? labelsIn(line).some(label => known.includes(label)) : LABEL.test(line)
      if (!candidate || labelled || covered(text, queue)) continue
      found.push(text)
    }
    if (found.length >= limit) break
  }
  return found
}

/** The labels `reply` claims as the session's, outside code, quotes and descriptions of elsewhere, that aren't in `known`. */
export function unknownLabels(reply: string, known: readonly string[]): string[] {
  const missing = new Set<string>()
  for (const raw of prose(reply)) for (const label of claimedIn(raw.trim(), known)) if (!known.includes(label)) missing.add(label)
  return [...missing]
}

// A code block that holds shell: its info string names a shell, or a terminal session.
const SHELL_LANG = /^(?:sh|bash|zsh|ksh|fish|shell|console|shell-?session|terminal)$/i
// A line in an unlabelled code block that reads as a command: a prompt, sudo, a && or || chain,
// or a word with flags after it.
const COMMAND_LINE = /^\s*(?:\$\s|sudo\s)|\s(?:&&|\|\|)\s|^\s*[\w./~-]+\s+(?:[\w./~:@=-]+\s+){0,3}--?[a-z]/i
/** A command line wider than this wraps in most panes and breaks when copied out of tmux. */
export const WIDE = 100

/**
 * The widths of the command lines in `reply`'s code blocks wider than `width` columns: every line
 * of a shell block, and lines of an unlabelled block that read as commands. Claude Code wraps a
 * long line in a code block at the pane width, and copying it from tmux turns the wrap into a line
 * break, so the pasted command runs in pieces.
 */
export function wideCommands(reply: string, width = WIDE): number[] {
  const widths: number[] = []
  let lang: string | null = null
  for (const raw of (reply ?? '').split('\n')) {
    const fence = /^\s*(?:```|~~~)\s*([\w-]*)/.exec(raw)
    if (fence) {
      lang = lang === null ? (fence[1] ?? '') : null
      continue
    }
    if (lang === null) continue
    const columns = [...raw.trimEnd()].length
    if (columns <= width) continue
    if (SHELL_LANG.test(lang) || (lang === '' && COMMAND_LINE.test(raw))) widths.push(columns)
  }
  return widths
}

/** What the model is told on its next turn about the wide command lines in its last reply. */
export function wideNudge(widths: readonly number[]): string {
  const widest = Math.max(...widths)
  return `session-decisions: your last reply had ${widths.length === 1 ? 'a shell command line' : `${widths.length} shell command lines`} over ${WIDE} columns in a code block (the widest ${widest}). Claude Code wraps it at the pane width, and copying it from tmux turns each wrap into a line break, so it runs in pieces. If the user still needs to run it, hand it over again as a script with one short line that runs it, or split it into lines well under 80 columns with \`\\\` continuations.`
}

/** What the model is told on its next turn about the hand-offs its last reply left unqueued. */
export function nudge(lines: readonly string[], missing: readonly string[] = []): string {
  const named =
    missing.length === 0
      ? []
      : [
          `Your reply named ${missing.join(', ')}, but ${missing.length === 1 ? 'no such item is' : 'none of those are'} in this session's queue. If ${missing.length === 1 ? 'it is one' : 'one is'} of this session's items, it was never created: create it now (the hook picks its label, which may differ, so correct the reference in your next message), and from here on make the tool call before writing the sentence that names a label. Ignore a label you quoted from an issue, a PR, a document or another session; don't create an item for it.`,
        ]
  return [
    'session-decisions: your last reply handed these to the user without a queue entry:',
    ...lines.map(line => `- ${line}`),
    ...named,
    'Anything still waiting on the user goes in the decision queue, even when it never blocks your own work and even when you could do it if asked: a secret or credential to put somewhere, a prompt or key to approve, access only they have, a merge, a step on their hosts, a decision. Describing it in prose, or in a summary, does not queue it. TaskCreate (a deferred tool; load it with ToolSearch first), or decision_add only when TaskCreate is not in this session. Ignore a line that is not a hand-off, or one the user has already dealt with.',
  ].join('\n')
}
