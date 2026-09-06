# Anastomosis — the settled rules

**Single source of truth.** If a rule is not written here, it is not settled: read the code, decide, and add it. Every rule is one sentence a reviewer can check a diff against. History lives in `CHANGELOG.md` and `git log`, never here and never in a docstring.

Rules marked `#NNN` were paid for by a real defect. The number is the receipt.

---

## 1. PHI

1. No real patient data enters this repository. Fixtures are synthetic (`feedface-` ids, 555 phones, never-issued SSNs) or Synthea. `tools/phi_scan.py` enforces it in CI.
2. Nothing patient-derived is ever logged, raised, printed, or embedded in a message: not a name, value, path under an export, cell, row, selector text, search text, banner text, or dialog text. Log element names, LOINC codes, counts, booleans, hashes and enumerated codes only. An error path logs `exc_tag(exc)`, the type name, never `str(exc)`, because an exception message embeds the input that raised it. `docs/AUDIT_LEDGER.md` §11 names the sites that still violate this; each closes in the slice that touches its file. A refusal that must correlate a claim across a batch names the record by its position in load order or a run-scoped surrogate id (`safe_log_id`), never by filename or value.
3. A summary the operator sees (learn proposals, pack summaries, run manifests, verify coverage) may carry column names, type labels, counts, masked shapes, static template text, profile hashes, state names, and operator-chosen paths — never a cell value, never an export path echoed back.
4. The one written exception to rule 3: the QA report may quote chart values, because it is written only inside the hardened output directory. Loggers still get verdict counts only. A finding that can travel into a run-level summary outside that directory (`RecordCoverageCheck`) names kinds and counts only.
5. The one soft spot, stated so it is not mistaken for a guarantee: `packgen` promotes text that recurs across samples; two samples sharing a patient value would recur. A **single-sample** run emits no sample-derived text at all; multi-sample runs rely on the operator confirming the samples are distinct patients (`#200`). The wizard asks. Code does not verify. Raw sample text goes to `UNPLACED.txt` only and never into `template.html`.

## 2. Identity — the one wrong-match defence

6. Every presence check of a name, date, or value on a page anchors on word/number boundaries, never raw substring. `"98"` never matches inside `"98.6"` or `"1980"`; `"Ann"` never stands alone in `"Mary-Ann"`; `"Brien"` never in `"O'Brien"`. A wholly ideographic name part (CJK, Hangul, kana) also matches its flush-joined form in either part order, because those scripts render with no separator. `core/identity.py` is the only implementation.
7. A canonical id is the identifier the source stated, taken whole. HL7 `II` is the pair `(root, extension)`; `identity_from_ii` in `core/ccda_codes.py` is the only place the tree turns one into an id (`#404`, `#412`). Whether a bare non-GUID root names the instance is an argument with a stated reason: yes for a patient (one `recordTarget`) and an organization (re-stated on purpose; conflicting fields raise), no for an encounter and every clinical fact (many per document, no guard). A bare GUID root standing alone is trusted for any kind.
8. A clinical fact's id is the `<id>` its source stated, else its document position — never a value minted at parse time (`#405`). Two loads of one document produce byte-identical bundles.
9. Two documents that state different identifiers for one patient are unioned, never treated as a disagreement. Two encounters sharing one `<id root>` fold only when their fields agree; contradictory fields stay separate so the collision still surfaces (`#393`). The record fold refuses to run rather than quietly leave a new kind of field behind.
10. A measurement links to an encounter only when it carries no source-stated `encounter_id` and exactly one encounter falls on its calendar date. Otherwise it stays unlinked and is graded against the record summary.
11. A rendered PDF is attributed to a patient by the render-time index only, never by a filename prefix; two patients can share both names.
12. The browser resolver matches a patient by exact rendered name **and** DOB. Zero matches returns nothing; more than one exact match is a permanent failure. It never guesses.
13. An upload dialog's prefilled patient and document date are read back and any mismatch is a permanent, non-retriable failure.

## 3. Writing files, naming them, hashing them

14. Every writer that must never leave a partial file writes `.NAME.<pid>.tmp` beside the target and `os.replace`s it; on any exception the temp is unlinked. `core/atomic.py` is the only implementation; the ledger names the two forks that still exist and the slice that closes them. Bytes are written as bytes — text-mode newline translation once made every Windows mapping fail its own trust hash.
15. An atomic write sweeps temps beside its target only for pids the kernel positively reports dead; anything unreadable is treated as alive.
16. Every file or byte digest comes from `core/hashutil.py`. A second chunked reader anywhere is a defect, and the ledger names the four that still exist: the manifest, the upload preflight and verify level L0 must hash a delivered file identically or their digests disagree.
17. `safe_name` and `budgeted_name` in `core/textutil.py` are the one definition behind every delivered filename. A name cut to fit a budget is tagged with sixteen hex characters of its sha256, never fewer, against the birthday bound.
18. Every output directory is created `0o700` (POSIX) or with NTFS inheritance removed and access limited to the user, SYSTEM and Administrators (Windows), and the Windows result is verified by reading the DACL back and failing unless every ACE is an Allow for one of those three. A filesystem without ACLs gets a one-line warning. Every output root gets a PHI warning README regardless.
19. A second `anast` against the same output directory fails fast on a kernel advisory lock (`.anast.lock`); release on crash is the kernel's, not ours.
20. Deterministic outputs: `upload_manifest.json` and `run_manifest.json` carry no clock and no random; keys sorted, items sorted by their key. Every timestamp the tool writes comes from `core/clock.py`, so a run under `SOURCE_DATE_EPOCH` is byte-reproducible. The one exception is the upload ledger's `started_at`, which orders resumable runs and must stay monotonic; it reads the wall clock and is never part of a deliverable.

## 4. Packs and trust

21. Pack discovery order is `--pack-dir` → per-user pack dir → built-ins; first definition wins; a broken pack is reported unavailable with a diagnosis and never takes discovery down. Destination packs are discovered in the same order by the same walk.
22. External and per-user packs run only with explicit consent and only at the content hash they were trusted at (`context.py`, `template.html`, `pack.yaml`, in that order, the same bytes the loader executes). A changed hash is unavailable, not re-trusted.
23. Non-built-in `context.py` runs against restricted globals: no `open`, `eval`, `exec`, `compile`, `input`, `globals`, `print`; `__import__` from an allowlist. Anything else is refused **by name**. This is not a security boundary and is never described as one; it turns silent capability into loud refusal.
24. Built-ins are exempt from the sandbox by origin (decided in `reconstruct/packs.py`), never by anything a pack says about itself.
25. An external pack ships assets as `data:` URIs inside the trusted manifest, because it cannot open files.
26. Every render writes `render_provenance.json`: the pack's identity and a sha256 of every file under its root, `__pycache__` excluded, unreadable files recorded as `UNREADABLE`, and a `swapped_templates` list naming any template that changed between measurement and render.
27. A destination pack with any selector still at the `DISCOVER` placeholder refuses to run rather than guess the DOM.

## 5. Learn (one capability: a format from a sample table, a layout from sample PDFs)

28. The CLI and the GUI run the identical analyze → confirm → emit flow through one command layer that returns structured data and never prints. `confirmed=False` analyzes and writes nothing. `confirmed=True` **is** the consent.
29. `confirmed=True` for a layout emits the draft, records its hash in the trust store, and returns dir/hash/`DRAFT.md`; if the hash cannot be recorded the whole draft is discarded, never left as an untrusted stand-in. For a format it round-trips the mapping against the example to prove no column is dropped, then saves owner-only.
30. A mapping target is drawn from the closed set in `core/model_paths.py`. Anything outside that set is never a mapping target.
31. A saved format is registered in the running process at once; a name colliding with an installed source is refused **before** writing (`SourceIdReserved` for a built-in, `SourceIdInUse` for the operator's own earlier work).
32. The destination is resolved before analysis and recorded as a profile hash; a mapping taught for one destination refuses to run at another, or at the same one after its profile changed, and names both ends.
33. `packgen` stores an opaque sample index, never a path. A section candidate is promoted only when it recurs in more than one sample; single-sample candidates stay low-confidence.
34. OCR spans carry provenance and confidence, declared in the manifest, `DRAFT.md`, the quarantine file and `OCR_EVIDENCE.md`. OCR is layout evidence, never clinical truth.
35. The same analysis produces byte-identical pack files: sorted keys, fixed float formatting, deterministic order.
36. Drafts go under the per-user packs dir unless `out_dir` is given — never a CWD-relative `packs/`.

## 6. Archive and delivery

37. The read → model → render → check path makes no network call. Only `deliver/fhir_api` and `deliver/browser` talk to a network, and only to the destination the operator named.
38. Every archive page opens from `file://` with zero outbound requests, declares a strict CSP, and references assets by relative path only.
39. The only `<script>` blocks are `type="application/json"` data on `index.html` and one self-served `assets/anast-index.js`. No inline executable JavaScript.
40. Patient directories are named by id only; renaming a patient never moves files. `index.json`'s `search` field is exactly the lowercased concatenation of name, DOB, chief complaints and note text.
41. `FhirEndpoint` refuses plaintext `http` except to loopback. The bearer token never appears in `__repr__`, a log, or a traceback. Errors carry status code and resource type only — never the `OperationOutcome` body or the URL. A destination-attach seam takes the token as a constructor parameter only and never reads it from the environment or argv itself.
42. FHIR status maps to one taxonomy: 401/403/404/other 4xx → permanent; 408/429/5xx/transport → transient. Every redirect, same-origin included, is refused rather than followed with the bearer re-attached. A URL the server names (a `next` link, an attachment `url`) is followed only when it resolves to the same origin as the endpoint.
43. The FHIR route inlines the document in the request body, so it weighs the file's actual size on disk before reading it and raises `PayloadTooLarge` above `max_payload_bytes`; the manifest's advisory size is never trusted for this, and the refusal names the item key and the two sizes, never the filename.

## 7. Verify and upload

44. A missing manifest, unsupported version or missing key raises `ManifestError` rather than starting on half the data. `file_path` is stored relative to `out_dir`; a stored path that would climb out is refused. The manifest builder raises on a render file that is missing, a source document whose bytes changed since the record hashed it, or a skiplist path that does not exist.
45. The manifest and the tracking database live only inside the `0o700` output dir, because the ledger's `file_path` column is name-derived. The tracking schema has no column typed for demographics; `file_path` is never logged; its error columns hold exception type names only. Both are logged only as counts, version and exception type, and never committed. The tracking database runs `synchronous=FULL` deliberately.
46. From v3 the manifest records the route plan and gate outcomes that `assert_deliverable` refuses on; a v3+ manifest that declares gates but carries none refuses, and only a pre-v3 manifest is grandfathered with a warning. From v4 each item declares which of L0–L6 can honestly run over its bytes: a rendered chart gets the whole ladder, a source-paged item gets L0 re-hash and L1 exact page count, an opaque item gets L0 only. A level that cannot honestly run says so; it never passes.
47. Verify is on by default and fails closed: if PyMuPDF is missing and verify is on, the command refuses before touching the browser, and never files unverified.
48. A `Verifier` returns `None` to pass, raises `PermanentDeliveryError` to fail without retry, and raises `TransientDeliveryError` to retry. `verify_pre` and `verify_post` map their first failing level to `PermanentDeliveryError`; only L4's banner mismatch escalates to `WrongPatientError`, which aborts the whole run.
49. Verification never mutates the state it verifies. `LevelCoverage` carries counts and deduplicated skip reasons; `LevelResult.detail` and the run report never carry an item key, patient value, date, or path.
50. An unrecognised exception during an upload is retried as transient up to `max_attempts`; it is never promoted to a permanent failure on the first throw. A `ManagedDestination` is single-threaded by contract; each parallel worker constructs its own.
51. Upload state advances only through the legal-transition graph in `deliver/browser/states.py`; crash-recovery edges bypass it deliberately and are the one privileged path.
52. A CDP endpoint is loopback and an explicit port; anything else is a `ValueError`, never a warning, because a debug port is full remote control of a logged-in EHR session.
53. `run_manifest.json` records the three profile hashes, the export dir path and id, the pipeline version, and a state that moves only prepared → delivered → verified through `advance_state`, which refuses on any profile drift and names the profile and both digests. `migrate` writes `prepared` only; a migration is never reported delivered.
54. `deliver/verify/types.py` imports nothing from the project, so the manifest writer and the report writer can use its types without pulling the verifier in. The manifest writer decides `VerifyPolicy`; the ladder honours it; neither imports the other.

## 8. C-CDA

55. The parser is the specification for the exporter: every xpath, LOINC, template id and `xsi:type` the builder emits is what the parser traverses, and every element name the parser reads appears in the verified C-CDA R2.1 reference under `tests/fixtures/ccda/`. See `docs/CCDA_EXPORT.md` for what is deliberately not attempted (XSD validity, Schematron, ONC) and for the loss-narrative contract.
56. Whether a file is a CDA document is decided from the first XML start event lxml reports, never from a fixed byte window or the extension (`#384`). Documents are processed in filename codepoint order, never `Path` order, which case-folds on Windows. A document that fails to parse is named by its position in that order, never by filename.
57. The ledger walks the document independently of the parser. Every clinically meaningful construct and every `<entry>` ends in exactly one disposition — `structurally_parsed`, `narrative_preserved`, `unsupported`, `source_empty` — and `Conservation` raises if the books do not balance. The narrative-credit heuristic may under-credit, never over-credit, relative to an optimal assignment.
58. `structurally_parsed` is credited only when a canonical object's `provenance.source_id` names an `<id root>` the construct actually carries — never by section-code dispatch or by inferring from a non-empty collection. A root shared by two constructs is `unlinkable`, never guessed.
59. Every section's title and narrative are captured verbatim into `extensions["ccda:section:<loinc>"]` and its raw entries under `extensions["ccda:entries:<loinc>"]`, before any in-place rewrite of the tree, so the captured bytes match the source; a repeated section code gets `#2`, `#3` keys and never overwrites. Loss-narrative comparison reads a second, freshly hydrated copy of the tree; the verbatim mirror stays on the untouched one.
60. Header participations keep the role the document gave them. A `nullFlavor` maps to absent; a document that is not a `ClinicalDocument` raises `ValueError`; a role naming nobody stays nobody.
61. An encounter with neither type nor note is still an encounter and gets an explicit `nullFlavor="NI"` (`#402`). A `51899-3` section carrying this repo's own export stamp round-trips deduplicated: the merge takes the maximum generation and concatenates entries, never deduplicating across two source records of one patient. An unstamped `51899-3` is ordinary third-party narrative.
62. The corpus pin — `tools/ccda_corpus.py --ledger --count 6144 --seed 7 | sha256sum` — is a byte-identity invariant on the ledger. It moves only deliberately, with the reason in the PR.

## 9. Tabular sources and dates

63. Every table mapping declares the columns it consumes; every other valued column lands in `extensions` under the source's namespace. Nothing is silently dropped.
64. Where a vendor's schema brief says "could not determine", the adapter raises or routes to extensions. It never invents vendor semantics. A compressed blob with an unknown algorithm raises `NotImplementedError`.
65. Only the current version of a versioned row is used structurally; closed versions ride in extensions, never dropped.
66. A row with no owning patient is quarantined to `quarantine.json`, never merged into a neighbour (`#247`).
67. `core/timeutil.py` is the only date parser for vendor text (the ledger names the learned-source transforms' own `strptime` as the fork that still exists). A year-1 date (`1/1/0001 12:00:00 AM` and its spellings) and a bare `0` parse to `None` as the SQL sentinel they are (`#385`); a naive datetime is taken as UTC.
68. Every stage reconciles what it was offered against what it produced through `core/conservation.py` and refuses on any difference; a count derived from the thing it audits is never accepted as the audit. `qa/` is the one stage without it today (ledger §11).

## 10. Destinations

69. Every capability in `destinations/registry.yaml` whose kind is not `none` or `unverified` carries evidence: an http(s) `source_url` and an ISO `verified` date. An uncited claim fails validation; `unverified` never routes. Evidence is re-verified quarterly.
70. A destination routes only by what its registry entry's cited capabilities say: an API kind the tool has a client for routes through `deliver/fhir_api`, `browser: pack` routes through the shipped pack, and `none` or `unverified` routes nowhere while staying visible to the operator. Which destinations fall in which group is data in `registry.yaml`, never a constant in code.

## 11. CLI and GUI

71. The guided session hands argv to the real Typer app through one code path; there is no second command implementation behind the prompts. Its copy is linted for banned words.
72. Every option the CLI accepts reaches behaviour. An option that changes nothing observable is removed, not documented.
73. `WEBVIEW2_USER_DATA_FOLDER` is never consulted; the WebView2 debug flag is never set in normal operation.
74. Every `GuiApi` method has a caller in the shipped JavaScript and every `pywebview.api` call names a real method; `tests/gui_e2e/test_bridge_surface.py` pins the join in both directions.

## 12. Imports and startup

75. `import anastomosis.deliver` and `import anastomosis.cli` stay cheap: no lxml, jinja2, Playwright or sqlite3 at module load. Nothing under `deliver/browser` imports Playwright at module load, so the package imports without the `deliver-browser` extra. PyMuPDF, `sourcelearn` and the learned-source package import lazily inside their entry functions; a minimal install imports every module cleanly. Importing `anastomosis.gui` never requires the `gui` extra: pywebview is imported inside `gui/shell.py`'s `launch` only.
76. `core/` imports nothing from `deliver`, `pipeline`, `reconstruct`, `sources`, `qa`, `destinations`, `packgen` or `gui`. The command layer lives in `commands/`, not in the primitives package. Until the slice that moves it lands, the nine modules `docs/AUDIT_LEDGER.md` names are the known exceptions.

## 13. Gates (never weakened to pass)

77. `bash tools/check.sh` is the gate: preflight, ruff, format, mypy, pytest, complexity ratchet, prose ratchet, guard count, PHI scan. Exit code read unpiped.
78. The complexity baseline and the prose baseline are ratchets: a commit may shrink them, never grow them. Regenerate in the commit that improved things.
79. No test is skipped, disabled, quarantined or `xfail`ed to get green. The count of distinct `#NNN` regression references in `tests/` does not decrease.
80. A mutation that proves a guard bites is made in a disposable copy of the tree, never the working branch.

## 14. House rules (how code is written here)

81. **Search before you write.** Before a new function, class, helper or constant, search `src/` and `tests/` by what it does, what it returns, and what the neighbours call it, and say in the PR what was searched. A second implementation of an existing thing is a defect, not a style choice.
82. **A branch needs a receipt.** A defensive branch, a configuration flag, a fallback or a test exists for a filed `#NNN`, a committed vendor sample, or a spec clause, and names it. Speculation is cut. A test is happy, regression or hypothetical; a hypothetical test goes with the branch it hypothesised about.
83. **A comment says what the code cannot**: a why, a constraint, a surprise. Never what the line below does, never the task, issue or history; `CHANGELOG.md` and git hold the story, this file holds the rule. A module docstring is at most 10 lines; a function or class docstring at most 5 unless it states a contract and says so; no history words. `tools/prose_gate.py` enforces the caps and the ratchet.
84. **Copy mechanism, share knowledge.** A clinical mapping, a code system or an identity rule lives in exactly one place. Five self-contained lines with no shared meaning may be copied. Unsure which it is: copy, and extract on the third occurrence with the same semantics, never the first.
85. **One concern per PR**, under roughly 400 hand-edited lines. Refactor and feature never share one. Deleted code takes its tests with it. A slice under 200 lines of delta is churn and folds into a neighbour.
86. **Touched, not inferred.** A run claim carries its exit code read unpiped, a file claim its bytes, a UI claim the rendered control. "Should work" is not a sentence in a report.
87. **Decisions go global.** A policy set for one adapter, deliverer or frontend is set for all of them, in the same PR or the next one with an issue naming it. Two adapters with opposite answers to one question is a defect.

## 15. Rules the prose sweep rescued

Each of these lived only in a docstring or comment until the sweep (S-1) cut it to one line and moved the rule here. They sit at the end so no earlier number moves; the sweep cites numbers in place.

88. A delivery routes cheapest-first: vendor API, then C-CDA import, then browser automation, never in another order.
89. Two different source ids that would take one delivered name raise `DeliveredNameCollision`; they are never merged into one slot.
90. `VERIFYING_PRE` runs the wrong-patient banner check before the duplicate scan, and the duplicate scan is trusted only after that identity check passed.
91. The CLI and the GUI show `SHARED_MACHINE_WARNING`, the same text, before attaching over CDP, because loopback is reachable by every local user of a shared machine.
92. A media type is what the source declared, never sniffed from bytes: the Practice Fusion page counter, the C-CDA `<nonXMLBody>` `@mediaType` reader and the upload manifest all follow it.
93. Every delivered file and directory is named by id (patient id, artifact id), never by patient name or the source's own filename; a C-CDA export is the artifact most likely to leave this tool's directory control.
94. A C-CDA delivery that cannot write every source document a record names raises `ArtifactNotDelivered` before reporting success (`#373`).
95. A construct CDA gives no `<id>` to is credited by exact stated content (no case-fold, no trimmed padding), never by re-running the parser's mapping; N such constructs against M matching objects credit `min` by multiset intersection, each object answering for one construct and then spent.
96. A run-of-zeros timestamp the parser reads as absent is also credited on the record under `ccda:timestamp_named_no_instant`, so the degradation from "stated a sentinel" to "absent" is never silent.
97. A ledger verdict states a disposition, never a cause: a construct that moves from credited to uncredited because of a shared id root is the instrument's blind spot, never a claimed adapter failure.
98. A narrative citation resolves to exactly one cell, the innermost identified element wrapping the cited text; ties between competing claims break on content (how much is claimed, then the cited names as a set), never on document order.
99. Learned-source discovery never raises: a directory without `mapping.json`, a malformed, un-reviewed or id-colliding mapping is skipped with a name-only diagnosis, the same way rule 21 treats a broken pack.
100. An attachment stem that matches more than one file in an export is dropped from the index, never resolved to an arbitrary match: an unresolved attachment is recoverable, a wrongly guessed one is not.
101. A cross-patient join that resolves one shared fact (an insurance plan's type) reads that one fact from another patient's row and never that row's other columns.
102. Every frontend field that names a folder or a file goes through `core/output.py`'s `typed_path`, never a bare `Path(arg)`; `tests/unit/test_gui_console_paths.py` walks each console's AST to pin it.
103. `core/timeutil.py`'s `all_date_spellings` is the one list of date spellings the delivery verifier (L2/L3) and QA's `DataIntegrityCheck` accept, so they cannot disagree about which rendering counts as present.
104. A command the operator declines at its own confirmation exits 0 like success and calls `core/outcome.py`'s `declined`; `take_declined` is a destructive read, so a stale outcome never frames the next run.
105. Whether a stream is a terminal is asked with `isatty()` directly, never Rich's `is_terminal`: `FORCE_COLOR` and `TTY_COMPATIBLE` make the latter say yes for a piped file, and an unattended run would wait on a prompt nobody can see.
106. FHIR export carries a canonical field with no clean R4 home as `urn:anastomosis:field:<name>` and the source's own `extensions` dict as `urn:anastomosis:ext`; `provenance` is local lineage and is never exported. Ingest reverses exactly that.
107. No `cli_commands` module imports `anastomosis.gui` and importing `gui` never imports `cli_commands`; `tests/unit/test_import_boundaries.py` pins the peer-frontend boundary.
108. The upload console never closes the operator's own browser, only the tool's ledger handle; a stop is honoured at item boundaries, never mid-item; a restart resumes from the ledger, which rewinds mid-flight items and never re-drives terminal ones.
