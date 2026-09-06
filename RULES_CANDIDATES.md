# Rule candidates found only in a test docstring (worker T1, slice S-1)

- A command records its own declined/confirmed outcome
  (`core.outcome.declined`/`take_declined`) rather than letting
  `_dispatch`'s single exit-code integer collapse "declined" and "done"
  into the same value, since only the command itself knows which
  happened — tests/unit/test_guide.py:565.
