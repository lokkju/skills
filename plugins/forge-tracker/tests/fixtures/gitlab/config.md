---
forge: gitlab
api_base: https://gitlab.example.org/code/api/v4
labels:
  needs_human: needs-human
  decision: decision
  decision_made: decided
  blocked: blocked
  agent: ~
tiers:
  labels: [P1, P2]
  milestone_prefixes: ["Tier 1", "Tier 2"]
blocking: relates_to+blocked
ruling_pattern: '^Ruling \((\d{4}-\d{2}-\d{2})\):'
report_tags: ["v*"]
---
