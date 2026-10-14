# Changelog

## forge-release--v2026.10.14.1 (2026-10-14)

### Features

- **forge-release:** github-app-create --public and --user-token ([f496e1a](https://github.com/lokkju/skills/commit/f496e1ab213cc1d2c335b1fbfca0484dd2c84751))
- **forge-release:** github-app-create --to sops writes the key into a SOPS Secret file ([3ee21ae](https://github.com/lokkju/skills/commit/3ee21aec7f5c4ae983fda5c67bc0a0ce463e82d8))
- **forge-release:** github-app-create --port and --no-browser for remote hosts ([74d9df6](https://github.com/lokkju/skills/commit/74d9df667ebd188aaec1b5955ce4fea9e01e9db4))
- **forge-release:** add github-app-create command ([a477f45](https://github.com/lokkju/skills/commit/a477f45972c0d123a45fd761af33fde5c83779d2))
- **forge-release:** store App credentials with gh, pulumi or a 0600 key file ([036f89f](https://github.com/lokkju/skills/commit/036f89f3f34c00e31bafdf1ce47bb9da519643cc))
- **forge-release:** convert the manifest code, sign App JWTs, poll for the installation ([9166510](https://github.com/lokkju/skills/commit/91665103d588cac7e3f3019e463102c7e2034dde))
- **forge-release:** build the App manifest form and local callback server ([06795ee](https://github.com/lokkju/skills/commit/06795ee7d7f48f30de3b1bb75ef81a527d9e540c))
- **forge-release:** add plugin manifest and marketplace entry ([2b3ea8d](https://github.com/lokkju/skills/commit/2b3ea8dce13dd33341580deed3a17f1dc90edc2f))

### Fixes

- **forge-release:** never lose the private key to an existing key file ([c8210f3](https://github.com/lokkju/skills/commit/c8210f390973938fc871cb4290d54ec6459d580e))
- **forge-release:** resolve a relative --sops-file before running sops ([22c8e21](https://github.com/lokkju/skills/commit/22c8e21a25683d821f2683d820cd67f92be31fb4))
- **forge-release:** leave the webhook block out of the App manifest ([cb88e5f](https://github.com/lokkju/skills/commit/cb88e5f6cfaa4e3618fe96d22fa3363b8aac1e98))
