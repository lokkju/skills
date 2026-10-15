# Changelog

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
