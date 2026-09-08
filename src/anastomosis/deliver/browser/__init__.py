"""Browser delivery: the resumable upload pipeline (M2 item 10).

Migration mode's last-resort route: file reconstructed charts into a
destination EHR through its web UI when no vendor API or C-CDA import
exists. A state machine (:mod:`.states`), a SQLite ledger (:mod:`.tracking`)
and an on-disk manifest (:mod:`.persist`) let a killed run resume without
double-filing a chart. Re-exports nothing: each name comes from the module
that defines it, so one submodule costs one submodule (75).

No Playwright import at module load anywhere in this package (75)."""
