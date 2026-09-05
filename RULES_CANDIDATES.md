# Rule candidates surfaced during the W11 (tests/) prose sweep

One sentence each, with the file:line the rule was pulled from. The
orchestrator adjudicates these into `docs/RULES.md` or rejects them; this
file does not ship.

- A construct CDA's own schema never gives an `<id>` to (`assignedAuthoringDevice`,
  `informant`) is credited on its exact stated content instead of an id match,
  and content evidence must match exactly (no case-fold, no trimmed padding) —
  `tests/unit/test_ccda_ledger.py:334`.
- A credit pool (`ledger._KeyedPool`, `_MatchedPool`, `_Anchors`) must be spent,
  never merely queried, when it answers a claim: two identical claims against
  one stored fact grade as one credited and one lost, never two credited —
  `tests/unit/test_ccda_ledger.py:743`.
- A verdict that moves a construct from credited to uncredited because of a
  shared id root must never assert a cause ("dropped", "no place"): the loss
  is the instrument's blind spot, never a claimed adapter failure —
  `tests/unit/test_ccda_ledger.py:1083`.
- A narrative citation resolves to exactly one "cell": the innermost
  identified element wrapping the cited text, never the whole `<text>`, a
  container that holds every cell (table/list/etc.), nor an outer wrapper
  when an inner one exists; ties between competing claims are broken by
  content (how much is claimed, then the cited names as a set), never by
  document order — `tests/unit/test_ccda_ledger.py:1665` (and the
  citation-crediting tests through line 3034).

## Loose ends

(none found yet — updated as the sweep continues)
