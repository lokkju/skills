/**
 * The oldest Claude Code this plugin runs on. CI runs the tests on this release as well as the
 * current one, and reads it from this line, so keep it a plain string literal.
 *
 * 2.1.295 is the first release whose Button takes Text children (the accent `[more]`); 2.1.300 is
 * the release the README and the comments describe. Raise it whenever the plugin should use
 * something a newer release adds, rather than working around older ones.
 */
export const MIN_CLAUDE_CODE = '2.1.300'

/** `2.1.300`, `2.1.300-dev` or `2.1.300-dev.20260920...` as [2, 1, 300]; null for anything else. */
export function parseVersion(version: string): [number, number, number] | null {
  const found = /^(\d+)\.(\d+)\.(\d+)(?:[-+.\s]|$)/.exec(version.trim())
  return found ? [Number(found[1]), Number(found[2]), Number(found[3])] : null
}

/** Whether `version` is older than `minimum`; null when either isn't spelled as a release. */
export function isOlder(version: string, minimum: string = MIN_CLAUDE_CODE): boolean | null {
  const have = parseVersion(version)
  const need = parseVersion(minimum)
  if (!have || !need) return null
  for (let i = 0; i < 3; i++) {
    if (have[i]! !== need[i]!) return have[i]! < need[i]!
  }
  return false
}

/**
 * Why the plugin stays off on the engine `version` reports, or null when it can run. A version not
 * spelled as a release (a build from source) runs; one older than the minimum doesn't, and neither
 * does null, an engine whose `$.session.version()` failed.
 */
export function refusal(version: string | null): string | null {
  if (version === null) {
    return `needs Claude Code ${MIN_CLAUDE_CODE} or newer and couldn't read this one's version; its pane, footer, /decisions and checks are off. Run claude update.`
  }
  if (isOlder(version) !== true) return null
  return `needs Claude Code ${MIN_CLAUDE_CODE} or newer; this is ${version}. Its pane, footer, /decisions and checks are off until you run claude update.`
}
