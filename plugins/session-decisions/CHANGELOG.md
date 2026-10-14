# Changelog

## session-decisions--v2026.10.14.1 (2026-10-14)

### ⚠ BREAKING CHANGES

- **session-decisions:** footer count, sidebar and transcript cards ([8c4953e](https://github.com/lokkju/skills/commit/8c4953e2730f4df3d3d1c65e777f3256a56c3b9b))
  > the band above the prompt and the `N open` status
  > notice are gone; the footer count replaces both.
- **session-decisions:** rewrite as a TypeScript mod with a live panel ([a7800eb](https://github.com/lokkju/skills/commit/a7800eb9dee3d684ae1557c2965dd1421af65694))
  > needs Claude Code function hooks (off by default in
  > claude -p). The decisions.py CLI, the decisions-status statusline shim
  > and `/decisions statusline` are gone; the count is now the plugin's
  > own status entry.

### Features

- **session-decisions:** footer count, sidebar and transcript cards ([8c4953e](https://github.com/lokkju/skills/commit/8c4953e2730f4df3d3d1c65e777f3256a56c3b9b))
- **session-decisions:** show the recommendation as a tag ([9f25199](https://github.com/lokkju/skills/commit/9f2519938138ce80bc6453b998a8169c8903b20d))
- **session-decisions:** draw the queue as cards ([f0031d9](https://github.com/lokkju/skills/commit/f0031d9bbf342f5c14249a890cf2ec142b94ca9c))
- **session-decisions:** answer several items in one message ([2c2a914](https://github.com/lokkju/skills/commit/2c2a914410385c1a3b6ebe6efb9b63b84045fa2c))
- **session-decisions:** rewrite as a TypeScript mod with a live panel ([a7800eb](https://github.com/lokkju/skills/commit/a7800eb9dee3d684ae1557c2965dd1421af65694))
- **session-decisions:** add skill, /decisions command and README ([edc817e](https://github.com/lokkju/skills/commit/edc817e7214974723a2038afb1ca3f13e8b90f3a))
- **session-decisions:** add CLI for hooks, statusline and fallback ledger ([cfe3ea9](https://github.com/lokkju/skills/commit/cfe3ea910ecb0915035560cc66a7711a377a5cc2))
- **session-decisions:** number and check items, re-show them after compaction ([a72874c](https://github.com/lokkju/skills/commit/a72874c8bdc02a690dcabd610eb10fe0996f52f6))
- **session-decisions:** add session state, counters and fallback ledger ([735d8df](https://github.com/lokkju/skills/commit/735d8df73d9f575f212ea20475d9b8eec7c89009))
- **session-decisions:** scaffold plugin and parse item subjects ([7152be6](https://github.com/lokkju/skills/commit/7152be648837ba6648474f21ade2f978838fc557))

### Fixes

- **session-decisions:** settle and keep items when the task tools come and go ([bf5fee3](https://github.com/lokkju/skills/commit/bf5fee3fbf5fe002e4df8c1e13dc20d7917166e0))
- **session-decisions:** keep an item's label when TaskUpdate changes its subject ([5bbfb0e](https://github.com/lokkju/skills/commit/5bbfb0eec95e6ca96a0f944e561a142cb72f5a27))
- **session-decisions:** skip the store write for unrelated task updates ([27cb38b](https://github.com/lokkju/skills/commit/27cb38b0ddb2d73cfe719fe4c4305af21a9d9293))
- **session-decisions:** keep failed-hook items visible, per-session pointer ([576cd23](https://github.com/lokkju/skills/commit/576cd23f131e3cb51ed2b1e747e9fa1d895d52d1))
