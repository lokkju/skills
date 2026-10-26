# Changelog

## [session-decisions--v2026.10.26.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.25.1...session-decisions--v2026.10.26.1) (2026-10-26)

### Fixes

- **session-decisions:** tell the assistant what to do when the security plugin skips its hooks ([73db650](https://github.com/lokkju/skills/commit/73db6501bc4b49f7f3c4de92caa9178b37c60781))
- **session-decisions:** toast unqueued hand-offs at turn end when classic.Stop is skipped ([fa64629](https://github.com/lokkju/skills/commit/fa64629972b1c1383bffca459a1e6cbd2ad374d0))

## [session-decisions--v2026.10.25.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.23.1...session-decisions--v2026.10.25.1) (2026-10-25)

### Features

- **session-decisions:** stay off with a loud notice below the minimum Claude Code ([0e9c315](https://github.com/lokkju/skills/commit/0e9c315f298d53d7cd81f4517cd30b2d3ed45616))
- **session-decisions:** show the reconcile brief once, as the command's output ([61d04fa](https://github.com/lokkju/skills/commit/61d04faf7e44f93c43c3e7b07ec360f49ec87c62))
- **session-decisions:** reconcile the open queue against the session ([bbfb786](https://github.com/lokkju/skills/commit/bbfb786b7448d048716869f465b6f9b577a22b2c))

### Fixes

- **session-decisions:** a card that can't be built costs that card, not the whole pane ([f928d8c](https://github.com/lokkju/skills/commit/f928d8c743c051d9b0a08edb027d92aaf2127ae7))
- **session-decisions:** link GitLab issues to -/work_items/N ([1f152c2](https://github.com/lokkju/skills/commit/1f152c26fa377f101930773a7aff92b42e977135))
- **session-decisions:** keep items as a list in the reply when the plugin isn't loaded ([3233fbf](https://github.com/lokkju/skills/commit/3233fbf3fc6dd94e09ebbb32a3a4b125fdc5a230))

## [session-decisions--v2026.10.23.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.20.2...session-decisions--v2026.10.23.1) (2026-10-23)

### Fixes

- **session-decisions:** draw [more] in the accent color, underlined, not dim ([a03f077](https://github.com/lokkju/skills/commit/a03f077e0656e9e122efc8254820b92cbf531b1a))
- **session-decisions:** Back from the details view keeps the list's place ([b8bb2a0](https://github.com/lokkju/skills/commit/b8bb2a0ecf1454d582ddb116e79b98898cca17da))
- **session-decisions:** draw a queued item once, as one line in the transcript ([1c8e804](https://github.com/lokkju/skills/commit/1c8e804b06d2fca683e6ee264b2ebec221165fc7))
- **session-decisions:** don't flag a feature's output described as "it prints ... for you to run" ([5c4fdea](https://github.com/lokkju/skills/commit/5c4fdeac0f6118721876f563a337df13ce58dd55))
- **session-decisions:** lead the skill with actions only the user can take ([655abd7](https://github.com/lokkju/skills/commit/655abd751e000ad0316bd0a57849421290d5fb2e))
- **session-decisions:** flag user-only steps in prose and labels never queued ([378cf99](https://github.com/lokkju/skills/commit/378cf996249b57dfb2df49cad04958edb25c9ac3))

## [session-decisions--v2026.10.20.2](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.20.1...session-decisions--v2026.10.20.2) (2026-10-20)

### Fixes

- **session-decisions:** carry ledger-only queues across a session id change ([759c183](https://github.com/lokkju/skills/commit/759c183c6558ae7ef707e9498f741eb21e61c825))

## [session-decisions--v2026.10.20.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.18.1...session-decisions--v2026.10.20.1) (2026-10-20)

### Fixes

- **session-decisions:** show [more] details in a scrollable pane instead of the cut-off dialog ([910374c](https://github.com/lokkju/skills/commit/910374c05df1bc86da7574d707f1e8a8ca5a11a4))

## [session-decisions--v2026.10.18.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.16.4...session-decisions--v2026.10.18.1) (2026-10-18)

### Features

- **session-decisions:** inline [more] after the card text, shown only when the dialog has more (#86) ([227fa39](https://github.com/lokkju/skills/commit/227fa39e8c3e03f9c3daea223f076e1eef88732b))
- **session-decisions:** inline [more] after the card text, shown only when the dialog has more ([c8aaa2f](https://github.com/lokkju/skills/commit/c8aaa2fa9ebabf9404ef99c5e86e3d59a5d5099c))

## [session-decisions--v2026.10.16.4](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.16.3...session-decisions--v2026.10.16.4) (2026-10-16)

### Features

- **session-decisions:** per-repo forge override in git config (tracker.forge, tracker.web) ([d62632a](https://github.com/lokkju/skills/commit/d62632a421bfa77eea92de92fa623477735c21c9))

## [session-decisions--v2026.10.16.3](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.16.2...session-decisions--v2026.10.16.3) (2026-10-16)

### Fixes

- **session-decisions:** the hand-off nudge points at deferred TaskCreate before decision_add ([053c120](https://github.com/lokkju/skills/commit/053c120bda9ed267f594e87b1d1aa92ef8f00b97))

## [session-decisions--v2026.10.16.2](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.16.1...session-decisions--v2026.10.16.2) (2026-10-16)

### Features

- **session-decisions:** fit the hand-off toast on one line ([1edf6c7](https://github.com/lokkju/skills/commit/1edf6c7c8c05fa0b0f9aa3f3b176c3542aa59fd6))
- **session-decisions:** warn when a reply hands work to the user without queuing it ([debda95](https://github.com/lokkju/skills/commit/debda953bd997b2fed47f4f86b6d20410cdc8430))

### Fixes

- **session-decisions:** decision_close settles task-list items from a subagent without TaskUpdate ([4dcce7e](https://github.com/lokkju/skills/commit/4dcce7ee23e15c63215824238c590f6ac587ef54))

## [session-decisions--v2026.10.16.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.15.3...session-decisions--v2026.10.16.1) (2026-10-16)

### Features

- **session-decisions:** a forge_hosts option names the forge kind of a self-hosted host ([83b9bef](https://github.com/lokkju/skills/commit/83b9befa05a66fc1cf35990aa5f762aae4877377))
- **session-decisions:** link issue and pull request references to the repository forge ([4630271](https://github.com/lokkju/skills/commit/46302711e66eec5f9113dfe878aa1ce1c60f4a1e))

### Fixes

- **session-decisions:** draw cards for TaskCreate calls folded into a tool group ([f284f3e](https://github.com/lokkju/skills/commit/f284f3eb895863ef29a09a54cfcfa82be2cba05e))

## [session-decisions--v2026.10.15.3](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.15.2...session-decisions--v2026.10.15.3) (2026-10-15)

### Fixes

- **session-decisions:** show the whole item in the More dialog ([893ae8a](https://github.com/lokkju/skills/commit/893ae8aef2273e00c2ca4db69b7165e90a89abe9))

## [session-decisions--v2026.10.15.2](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.15.1...session-decisions--v2026.10.15.2) (2026-10-15)

### Features

- **session-decisions:** open More in a question dialog and give URLs their own line ([ec2dbde](https://github.com/lokkju/skills/commit/ec2dbdeb48935f803ff6a134d39a2e5a1b7349d0))

## [session-decisions--v2026.10.15.1](https://github.com/lokkju/skills/compare/session-decisions--v2026.10.14.1...session-decisions--v2026.10.15.1) (2026-10-15)

### Features

- **session-decisions:** render markdown in cards and expand long items ([09fd9ca](https://github.com/lokkju/skills/commit/09fd9cadc2468caaa898ba5dff9ce078ed302dad))
- **session-decisions:** have the skill revise open items when they change ([5207d61](https://github.com/lokkju/skills/commit/5207d611d52893ec5c142e363b363894285d1b2a))
- **session-decisions:** add decision_update and mark revised items ([0ad1794](https://github.com/lokkju/skills/commit/0ad17947c5c40a4d4ce63c395bbacecb45f5c232))

### Fixes

- **session-decisions:** tell the model to load a deferred TaskUpdate ([0091db6](https://github.com/lokkju/skills/commit/0091db6d03af1b5e0a58ef02b23bff23e1fa085b))
- **session-decisions:** keep the stored queue when the session id changes ([27b06e5](https://github.com/lokkju/skills/commit/27b06e5c359400de22e475f17dbd27f28a7d1dc8))
- **session-decisions:** treat the fallback ledger as a normal path in the skill ([964a252](https://github.com/lokkju/skills/commit/964a252893d1c9a964ca883253c1f11e0e2d5c63))
- **session-decisions:** drop the ledger tag from cards ([355ed65](https://github.com/lokkju/skills/commit/355ed653cc07321be83f1079f3611aa4d07e5a06))
- **session-decisions:** accept only real link tokens and catch self-numbered drafts ([757aab6](https://github.com/lokkju/skills/commit/757aab621f1de81cee76fd6eb57d6f934d66cfd8))
- **session-decisions:** accept (recommend: ...) and nested parentheses at any depth ([dc5145c](https://github.com/lokkju/skills/commit/dc5145cb6821e6a5be682c449762b80f8837afc8))

## session-decisions--v2026.10.14.1 (2026-10-14)

The first release yeet manages, and the move from SemVer to CalVer. Everything up to 2.0.1 shipped before this release; the git history has it.

### Fixes

- **session-decisions:** keep an item's label when TaskUpdate changes its subject (#7) ([5bbfb0e](https://github.com/lokkju/skills/commit/5bbfb0eec95e6ca96a0f944e561a142cb72f5a27))
- **session-decisions:** settle and keep items when the task tools come and go (#9) ([bf5fee3](https://github.com/lokkju/skills/commit/bf5fee3fbf5fe002e4df8c1e13dc20d7917166e0))
