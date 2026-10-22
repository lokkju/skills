# Changelog

## [forge-tracker--v2026.10.20.2](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.20.1...forge-tracker--v2026.10.20.2) (2026-10-20)

### Features

- **forge-tracker:** add a working-rules skill that records blocking as native links ([6cc2229](https://github.com/lokkju/skills/commit/6cc2229c951deb803bb6312aaf4b136132282151))

### Fixes

- **forge-tracker:** read issue numbers from titles and branches only by real references ([4483c31](https://github.com/lokkju/skills/commit/4483c31926ba9bd6b2e0468c3ac1eedafae22990))

## [forge-tracker--v2026.10.20.1](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.18.2...forge-tracker--v2026.10.20.1) (2026-10-20)

### Features

- **forge-tracker:** tell agents to record blocking as native links ([7d71d16](https://github.com/lokkju/skills/commit/7d71d16553ba40ac1ec5c6884f6c3f11434e5e91))
- **forge-tracker:** propose native links for blocking written as prose in /groom ([e5af74c](https://github.com/lokkju/skills/commit/e5af74c9969df316ee920edd8a5e6d35df7d21e0))
- **forge-tracker:** add link add|remove for native blocking links ([0673d28](https://github.com/lokkju/skills/commit/0673d28c508403344d931d514342c44d321e7b2b))

## [forge-tracker--v2026.10.18.2](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.18.1...forge-tracker--v2026.10.18.2) (2026-10-18)

### Features

- **forge-tracker:** /next judges untagged items and closes with a recommendation (#85) ([b472cdc](https://github.com/lokkju/skills/commit/b472cdcf987f455f0e974004d9485835248e5a7a))
- **forge-tracker:** close /next with an evaluated recommendation ([67a9854](https://github.com/lokkju/skills/commit/67a9854ae96897940fdd9ae353efb5427ec7f85b))
- **forge-tracker:** let the slate judge untagged items person-only in labelled repos ([2f470fb](https://github.com/lokkju/skills/commit/2f470fbab5fc8518a30305a90caa7d6d91b079ed))

## [forge-tracker--v2026.10.18.1](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.16.2...forge-tracker--v2026.10.18.1) (2026-10-18)

### Features

- **forge-tracker:** classify unlabelled repos honestly and create missing labels on approval (#80) ([73cd13a](https://github.com/lokkju/skills/commit/73cd13a142c9a52e851902a2dbc86a5ecd7217ab))
- **forge-tracker:** create missing labels during setup, on approval ([94c3688](https://github.com/lokkju/skills/commit/94c3688422740accfd7b2b2356815fba9eaf5e4d))
- **forge-tracker:** workspace catchup and per-clone tracker.* keys (#81) ([29cdc92](https://github.com/lokkju/skills/commit/29cdc92a82df6c210785c59ab388340c30547758))
- **forge-tracker:** catch up across a folder of repos in one report ([0cd28b0](https://github.com/lokkju/skills/commit/0cd28b053fcce6ba2831cdca4f6bf6e16cec56e7))
- **forge-tracker:** list your own ready PRs as yours to merge in catchup ([713d9d5](https://github.com/lokkju/skills/commit/713d9d5a2d9e3c8592f9e424ee5d6acbccbfe598))
- **forge-tracker:** read the per-clone tracker.web and tracker.forge git config keys ([64d82da](https://github.com/lokkju/skills/commit/64d82da354b5ded64a99c53d3264981dc81f2afd))

### Fixes

- **forge-tracker:** fit label setup into workspace catchup ([f300de1](https://github.com/lokkju/skills/commit/f300de14f865a04aeb82d7143111f5aa45fc329d))
- **forge-tracker:** offer and accept label creation for roles set to ~ ([d0dd625](https://github.com/lokkju/skills/commit/d0dd625b4f92fa4b6aa6b994752f7de3db7ede24))
- **forge-tracker:** call unlabelled issues unclassified, not agent-ready ([689f166](https://github.com/lokkju/skills/commit/689f1665142331452afe55f53f93815a5e79bca9))

## [forge-tracker--v2026.10.16.2](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.16.1...forge-tracker--v2026.10.16.2) (2026-10-16)

### Features

- **forge-tracker:** the /groom command asks before it writes anything ([6780b4f](https://github.com/lokkju/skills/commit/6780b4f06f317e126e62eca4df06973b8f4d0536))
- **forge-tracker:** /groom finds tracker drift and applies approved fixes through a write layer ([9bdb107](https://github.com/lokkju/skills/commit/9bdb1073097b33101c7b68329a61289053aef29a))

### Fixes

- **forge-tracker:** /groom never proposes deleting release and dependency bot branches ([3b756a0](https://github.com/lokkju/skills/commit/3b756a0f8803b30309ae9e8414ea98bc013a09ed))

## [forge-tracker--v2026.10.16.1](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.15.3...forge-tracker--v2026.10.16.1) (2026-10-16)

### Features

- **forge-tracker:** offer label setup on first use ([5a219c0](https://github.com/lokkju/skills/commit/5a219c0cecf70914ecd38b386ec9acc2e66470c4))

### Fixes

- **forge-tracker:** list open PRs with no open issue in /next ([c066643](https://github.com/lokkju/skills/commit/c06664360d17e9c9ac18b9422e8d61fc9727528e))
- **forge-tracker:** read an issue number from a branch only when it's clearly one ([d241da7](https://github.com/lokkju/skills/commit/d241da72123afe653d4258d9e92e2c719be8d2f9))

## [forge-tracker--v2026.10.15.3](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.15.2...forge-tracker--v2026.10.15.3) (2026-10-15)

### Features

- **forge-tracker:** /next on GitHub: facts, fingerprint cache, classification and the slate ([a133dda](https://github.com/lokkju/skills/commit/a133ddae04cfbe195c9a4a2e2dd83c1f8baf6277))
- **forge-tracker:** read the /next fingerprint and closing references on GitHub ([c8b8a0f](https://github.com/lokkju/skills/commit/c8b8a0f84a5c8a47277d3c204115c3b3eea11ab4))

### Fixes

- **forge-tracker:** fingerprint /next on open PR heads and check state, not pipeline time ([a0a6c64](https://github.com/lokkju/skills/commit/a0a6c647e46f392d8c04fd6b65e1f1ab7d6cfb41))

## [forge-tracker--v2026.10.15.2](https://github.com/lokkju/skills/compare/forge-tracker--v2026.10.15.1...forge-tracker--v2026.10.15.2) (2026-10-15)

### Fixes

- **forge-tracker:** always print the standing-backlog line in /catchup ([d5c6ff1](https://github.com/lokkju/skills/commit/d5c6ff1834c60a8e0da54f8c275488dcea1806ab))
- **forge-tracker:** say where the marker moves in the /catchup header ([4d6a100](https://github.com/lokkju/skills/commit/4d6a10088e384408d1af4b8d98409b803e672ab8))
- **forge-tracker:** list only open items as mentions in /catchup ([f5b6628](https://github.com/lokkju/skills/commit/f5b662822def5c46660400b154fce5b973b8435b))

## forge-tracker--v2026.10.15.1 (2026-10-15)

### Features

- **forge-tracker:** add the plugin, /catchup command and marketplace entry ([ba1e131](https://github.com/lokkju/skills/commit/ba1e131b7bec7d3df1efdd1a138e2f3ec92456e9))
- **forge-tracker:** add the catchup report and CLI ([2b3f69e](https://github.com/lokkju/skills/commit/2b3f69e523fab652402b4a513315f7ff6871c825))
- **forge-tracker:** add the GitHub read adapter ([f717c5e](https://github.com/lokkju/skills/commit/f717c5ecb5977e74017a64d4b5ed590c8f26bad5))
- **forge-tracker:** detect the forge and load the per-repo config ([4e52c08](https://github.com/lokkju/skills/commit/4e52c08bf15ae3c04c37a4982975bb8a7d231393))
- **forge-tracker:** add the read transport and its write guard ([e9cdeaf](https://github.com/lokkju/skills/commit/e9cdeaf55ddccc5fbf447df5c31b5ae1d462d95c))
