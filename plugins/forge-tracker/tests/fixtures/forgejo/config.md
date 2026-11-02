---
forge: forgejo
api_base: https://code.example.org/forgejo/api/v1
labels:
  needs_human: needs-human
  decision: decision
  decision_made: decided
  blocked: blocked
  agent: ~
tiers:
  labels: [P1, P2]
  milestone_prefixes: ["Tier 1", "Tier 2"]
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
report_tags: ["v*"]
---
