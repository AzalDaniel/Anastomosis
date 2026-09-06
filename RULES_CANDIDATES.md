# Rule candidates from the W4 prose sweep (sources/ccda)

Rules found in a docstring being cut that are not already in `docs/RULES.md`.

1. For a construct class CDA gives no `<id>` at all (`ID_LESS_CONSTRUCTS`), a
   parse is credited by matching stated facts against a canonical object's
   recorded facts, never by reconstructing the parser's own mapping rule; N
   constructs against M matching objects credit `min` by multiset
   intersection, one object answering for one construct and then spent.
   `src/anastomosis/sources/ccda/ledger.py:522` (`class _MatchedPool`).

2. A run-of-zeros timestamp the parser reads as absent (rule 67) is also
   credited on the record itself, under `ccda:timestamp_named_no_instant`,
   so the degradation from "stated a sentinel" to "absent" is never silent.
   `src/anastomosis/sources/ccda/parser.py:2021,2043`
   (`EXT_TS_NO_INSTANT`, `_record_zero_sentinels`).

## Loose ends

None found — no `TODO`, `FIXME`, or `XXX` in `parser.py`, `ledger.py`, or
`__init__.py`.
