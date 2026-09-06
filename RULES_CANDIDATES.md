# Rule candidates found only in a test docstring (worker T1, slice S-1)

- A command records its own declined/confirmed outcome
  (`core.outcome.declined`/`take_declined`) rather than letting
  `_dispatch`'s single exit-code integer collapse "declined" and "done"
  into the same value, since only the command itself knows which
  happened — tests/unit/test_guide.py:565.
- The CodeQL advanced workflow (`.github/workflows/codeql.yml`) triggers
  on push, pull request, and a schedule; the analyzing job holds
  `security-events: write`; every third-party action is pinned to a
  full 40-hex commit SHA; and the alert-mutating dismissal step runs
  only after code is pushed to `main` — tests/unit/test_codeql_policy.py:6.
- The CodeQL config (`.github/codeql/codeql-config.yml`) selects the
  `security-extended` suite and declares NO repo-wide exclusions (no
  `query-filters`, no `paths`/`paths-ignore`) — coverage is full-tree,
  suppression is per-site — tests/unit/test_codeql_policy.py:11.
- Every `# codeql[...]` suppression sits alone on its own line, carries
  a `PHI-BY-DESIGN` or `PHI-FREE-BY-CONSTRUCTION` rationale in the
  lines just above it, and lives in a file SECURITY.md's suppression
  policy section names, with the set matching exactly in both
  directions — tests/unit/test_codeql_policy.py:16.
