# Audit ledger — main 7738b26

The work-list for the refactor toward the eighth alpha. One row per file, one row per function that earned a look, one merge map per slice, and the rules the prose was protecting. Every number here was measured on the commit named above; the command or the file:line is beside it. Nothing here is a guess about what the code does.

Read it with `.claude/skills/first-principles-audit`: every row answers the six questions, and a slice does not start until its rows are adjudicated. **Verdicts:** KEEP · MERGE-INTO `<target>` · SIMPLIFY · CUT. **A capability never gets CUT.** CUT is for code on no real path or duplicating a survivor.

Top-down half by the architect (package level, import graph from `grimp`), bottom-up half by eleven package auditors (file and function level, `radon cc`, `rg` both directions), dead code by three tools in agreement, adjudicated by the orchestrator. The full per-package reports are the receipts and live beside this file in the session record; this ledger is what the slices cite.

---

## 1. Baseline, measured

| measure | value | command |
|---|---|---|
| `src/` lines | 52,070 | `find src/anastomosis -name '*.py' \| xargs wc -l` |
| of which prose (docstrings + comments) | 19,062 (36.6%) | AST docstring spans + tokenize comments |
| of which code | 25,429 | |
| prose ratio of record, `src/anastomosis` | 0.404 (prose ÷ non-blank lines; `tools/prose_gate.py`) | `python tools/prose_gate.py --write-baseline`; 1,749 docstrings over the cap, 294 history-word hits, both ratcheted |
| `tests/` lines | 64,002 | |
| GUI JS / with CSS+HTML | 4,809 / 6,986 | |
| vendored (HL7 CDA.xsl + PINNED) | 18,566 | floor, not a target |
| truly dead by three-tool agreement | 4 symbols, ~18 lines | vulture-100 ∩ deadcode ∩ coverage-zero ∩ grimp |
| real code no test reaches | 178 statements, 56 functions + 5 classes | coverage-zero but referenced |
| complexity blocks over threshold | 71 (58 C, 8 D, 5 E) | `tools/complexity_baseline.json` |
| distinct `#NNN` regression guards in `tests/` | 72 | `rg -o '#[0-9]{2,4}' tests/ \| sort -u`, five hex colours excluded (§13); the gate's floor |
| corpus pin | `390b6b99…` | `tools/ccda_corpus.py --ledger --count 6144 --seed 7 \| sha256sum` |

The plan's survey table was twenty commits stale: it said 48,665 src lines and 30.9% prose. Every file row in it is 100–520 lines low. The prose estimate in the plan is therefore conservative, and the code estimates are optimistic (§14).

**The headline the dead-code pass forces:** this codebase is not big because of unreachable code. It is big because the same thing is written several ways, because nearly every docstring is three to seven times the cap, because branches guard inputs nobody has produced, and because flat functions wear class, protocol and registry ceremony. The reduction is structural or it is nothing.

## 2. Architecture verdict

The five stages are real and each has exactly one home: read = `sources/*` · model = `core/model` + `core/fhir` · render = `reconstruct` + `packs` · check = `qa` + `deliver/verify` · deliver = `deliver/*` + `destinations`. Learn = `packgen` + `core/sourcelearn` + `sources/learned`. CLI = `cli.py` + `cli_commands`. GUI = `gui`. **No package is off the path and no package is a persona.** The two persona-shaped modules, `cli_commands/guide.py` (727) and `core/vesselmark.py` (645), are both on a driven path (`anast` bare) and are SIMPLIFY, not CUT.

The architecture's one defect is a layering inversion, not a surplus package. **`core` is not a core.** Nine of its modules (`commands`, `migrate`, `migration_status`, `packinit`, `profiles`, `selfcheck`, `source_init_command`, `sourcelearn`, `upload_command`; 2,693 lines) import downward into `deliver`, `pipeline`, `reconstruct`, `sources`, `qa`, `destinations`, `packgen` and `gui`. That is the command layer living in the primitives package, it is why the import graph shows `core → gui`, and it is why every later slice reads its dependency direction wrong. It gets its own slice (S-2a) before slice 3. RULES.md 76 states the target.

Second structural finding: `packs/practice_fusion_soap/context.py` (1,089 lines) is a fourth "model → display strings" recipe beside `archive/templates.py`, the C-CDA narrative builders and `qa/checks`. The plan had no slice for it; it folds into S-5 through `reconstruct/packctx.py`, the sanctioned shared surface.

## 3. Package rows

| path | lines | verdict | reason | what falls into place |
|---|---|---|---|---|
| `pipeline.py` | 1,678 | SIMPLIFY | 41 functions in one module; the record fold (505–830) is a separate concern from the stage driver; `run_pipeline` 193 lines; `_run_qa_stage` is 79 lines of docstring over ~40 of code | `pipeline/run.py` + `pipeline/fold.py`; the package `__init__` keeps every name |
| `cli.py` | 624 | SIMPLIFY | three destination-attach seams that exist only for monkeypatching (§8) | one `attach=` parameter on `run_upload_command` |
| `core/` (27 non-model, non-fhir modules) | 8,362 | SIMPLIFY | primitives and the command layer in one package | the nine outward-importing modules move to `commands/` (S-2a) |
| `core/model` | 663 | KEEP | one implementation, leaf, no simpler alternative | — |
| `core/fhir` | 1,450 | MERGE-INTO one field table | `export.py` (871) and `ingest.py` (555) name the same twelve entities; a 79-row field inventory exists (§6, S-4) | `to_bundle` and `from_bundle` walk one table |
| `sources/` (base, `_rowutil`, `__init__`) | 336 | KEEP | a dict plus a protocol is what a veteran writes | — |
| `sources/ccda` | 4,983 | KEEP, prose SIMPLIFY | medplum's converter is 11,342 lines: honest size; prose 42.6%; only `parse_document` reaches CC 17 | a section-descriptor table drives its dispatch (S-9) |
| `sources/pf_tebra` | 2,479 | SIMPLIFY | nine entity mappers are already pure field tables; the joins, refusals and derived values around them cannot be tabled | the nine simple entities take `learned/spec.py`'s table shape (S-10) |
| `sources/fhir_r4` | 1,719 | SIMPLIFY | consumes the S-4 table's path constants, keeps its walker: the consumed-sub-path residue (lines 283–460) is the losslessness contract and has no counterpart in export or ingest | — |
| `sources/oracle_ehi` | 1,109 | SIMPLIFY | Patient and Encounter are field tables; CLINICAL_EVENT dispatch is a classifier, not a map | same table shape for the simple entities |
| `sources/learned` | 1,478 | KEEP | `interpreter.py` is already the table-driven mapper the other two should become; `spec.py` is the table | it is the model, not the target |
| `deliver/` (`_shared`, `render_index`, `router`, `__init__`) | 717 | KEEP | `_shared` already holds every byte-identical mechanic; `router` is 201 lines of pure logic | — |
| `deliver/archive` | 1,071 | MERGE-INTO one deliverer | `ArchiveDeliverer` and `BundleDeliverer` differ in grouping; `templates.py` is already three Jinja templates | `grouping=` argument (S-5) |
| `deliver/bundle` | 463 | MERGE-INTO `deliver/archive` | same operation, different grouping | its `__init__` re-exports from the merged module |
| `deliver/ccda_export` | 2,267 | KEEP, extract a table | nine section builders mirror the parser's dispatch; bodies are genuine inverses | the descriptor table (S-9), never merged bodies |
| `deliver/browser` | 3,534 | KEEP, fix `__init__` | Tebra's upload half is 23,947 lines; this is seven times smaller. `__init__` eagerly re-exports 37 names that **zero** external callers import through it | an empty `__init__` (S-3); no `__getattr__` needed |
| `deliver/fhir_api` | 1,200 | KEEP | RULES.md 41–43 live here; one implementation | — |
| `deliver/verify` | 1,198 | SIMPLIFY | `_POLICY_SKIPS` in `composite.py:106` is already the level→check table; seven level classes hold no constructor state; `types.py` stays (four production importers, one guard test) | classes → functions behind the existing table (S-11) |
| `destinations` | 2,071 | SIMPLIFY | the registry is honest data with a validator; the only over-generality is three Python slot tuples duplicating the YAML keys, and a discovery walk retyped from `reconstruct/packs.py` | one discovery walk (S-12) |
| `reconstruct` | 2,341 | KEEP | pack trust-at-hash is a security-shaped rule with tests; `_load_pack_dir`/`_load_pack_snapshot` are same-shaped | one loader (S-12) |
| `packs` | 1,170 | SIMPLIFY | the PF context's `build_context` is CC 33 and 121 lines; formatters shared with nobody | shared formatters into `packctx.py` (S-5) |
| `packgen` | 3,916 | KEEP detector, SIMPLIFY emitter | detector (`ocr`+`extract`+`evidence`+`infer`, 2,528) is under docling's 2,210-plus honest size; `emit.py` (1,220) is string-built markup with quadruple-brace Jinja escaping | HTML and Markdown builders become template files (S-6) |
| `qa` | 1,286 | SIMPLIFY | already smaller than Tebra's QA (1,684 over nine checks); the defect is `_REGISTRY`, one loop populating it and one function reading it | module-level tuple; `wholepatient`'s two scope tables become an argument (S-11) |
| `cli_commands` | 2,783 | SIMPLIFY | `upload_cmd` 274 lines CC 22; `guide.py` is 727 lines of inline prompt strings | split by the numbered steps in §5; prompts to one table (S-13) |
| `gui` (Python) | 3,147 | KEEP, SIMPLIFY inside | consoles are thin over the command layer; `runs.py`'s two `_run_*_locked` share one five-step shape | one locked runner (S-13) |
| `gui/web` | 6,986 | SIMPLIFY | `wizard.js` is the Migrate run view, not a teach wizard; the two teach wizards share a scaffold and differ in the proposal (§6, S-7) | scaffold and two shell-level idioms shared; source.js's mapping table stays |

## 4. File rows

Only rows whose verdict is not plain KEEP are listed, plus KEEP rows that carry a finding. Every other file under `src/anastomosis` is KEEP with no finding; the per-package reports hold their rows.

### 4.1 sources/ccda

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `parser.py` | 2,667 | SIMPLIFY | `parse_document` (CC 17) is a nine-arm LOINC `if/elif` that a descriptor table replaces; 44-line module docstring; 124 function docstrings over cap |
| `ledger.py` | 2,063 | KEEP | zero `except` blocks; no function over 40 lines; 73-line module docstring. `_Facts.namespaced` (624) is dead: CUT with the file kept |
| `__init__.py` | 253 | KEEP | `_scan` and `load` are 10 and 32 lines of code under 34 and 25 lines of docstring |

Confirmed by `comm -12` on top-level `def` names: exactly nine functions share a name with `deliver/ccda_export/builder.py` (`_telecom`, `_addresses`, `_ts_date`, `_allergies`, `_medications`, `_immunizations`, `_measurements`, `_social_history`, `_encounters`), plus the `_conditions`/`_problems` pair under different names. `builder.py` imports nothing from `sources.ccda`; the pairing is enforced by naming and one round-trip test file. Eight of nine can be driven by one `SectionSpec(loinc, template_root, template_ext, entry_xpath, model_field)` row each; `_measurements` is already parameterized on both sides and is the proof. `_ts_date` is a codec pair, not a section.

### 4.2 sources, tabular and FHIR

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `pf_tebra/mapper.py` | 1,885 | SIMPLIFY | nine one-row mappers (`_map_condition` … `_map_document`, called at ~1851–1880) are `return Model(field=_s(row,"Col"), …)` blocks: table them. `map_export` (1670, 216 lines, CC 36) keeps the lossless accounting, the `_by` grouping, the SOAP-vs-simple branch, `_auto_bmi`, and `_skip_reason`: joins, refusals, derived values a table has no slot for |
| `pf_tebra/mapper.py:1557 _sha256` | 16 | MERGE-INTO `core/hashutil.hash_and_size` | fourth streaming sha256 copy; differs only by swallowing `OSError` into a logged `None` |
| `oracle_ehi/mapper.py` | 710 | SIMPLIFY | `_map_patient`/`_map_encounter` are field tables; CLINICAL_EVENT title-prefix dispatch is a classifier and stays code |
| `fhir_r4/mapper.py` | 1,586 | SIMPLIFY | `_code_in` (302) dead: CUT. `_observations` (774, CC 27) is BP-component expansion, one resource → N records; `records_from_resources` (1348, CC 33) partitions dangling/shared/ambiguous resources. Neither is a field lift. Real asymmetries against `core/fhir/ingest.py`: MedicationRequest → MedicationStatement here, → Prescription there (documented); RelatedPerson/Device read as Practitioner there, not here; `Goal` read here, never emitted by `export.py` |
| `fhir_r4/mapper.py:363,379 _date/_datetime` | — | MERGE-INTO `core/timeutil` | ISO partial-padding is the one variable; becomes an argument |
| `_rowutil.py` | 64 | KEEP | already the unification; nothing left to merge |
| `learned/interpreter.py` | 480 | KEEP | `_FieldPlan(source_path, target_path, transform)` is the shape S-10 gives the other two |

### 4.3 core/model and core/fhir

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `core/fhir/export.py` | 871 | SIMPLIFY → S-4 table | `_patient` 111 lines CC 30; carries two entities (record-level extras stashed on the Patient resource under `EXTRAS_NS`); `_actor` is a three-way dispatch on `ccda:entity`/`ccda:role` keys, so export is not source-agnostic |
| `core/fhir/ingest.py` | 555 | SIMPLIFY → S-4 table | `from_bundle` 62 lines CC 25; `_pref` (80) is a three-way merge rule reused six times with no export-side counterpart |
| `core/fhir/export.py:173 _date`, `ingest.py:88,92 _dt/_d` | — | MERGE-INTO `core/timeutil` | three tiny ISO converters |

The 79-row field inventory (entity · model field · export path:line · ingest path:line · coding system · converter) is in the package report and is the raw material for the S-4 table. Four asymmetries are recorded there: `sex` written to both `gender` and the extension but read from the extension only; allergy `category` and `severity` written structurally but never read back; `DocumentArtifact.mime_type` defaulting differently on the two sides. Ten things cannot be table rows (§6, S-4).

### 4.4 core, the rest

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `migrate.py` | 826 | SIMPLIFY | `_run_ccda_standard` 127 lines; `MigrationProfiles.__init__` (777) silently starts empty on a corrupt store; 38-line module docstring; `:810` atomic fork → `core/atomic` |
| `sourcelearn.py` | 783 | SIMPLIFY | `:682 _atomic_write` → `core/atomic`; `:317 _load_synonyms` swallows a broken `synonyms.json` with no log and degrades matching silently (§11) |
| `source_init_command.py` | 454 | MERGE-INTO `commands/learn.py` | same four-step shape as `packinit.py`; the confirm gate is byte-identical |
| `packinit.py` | 288 | MERGE-INTO `commands/learn.py` | see §6, S-6 |
| `selfcheck.py` | 255 | SIMPLIFY | nine near-identical `try/except Exception: AssetCheck(name, False, exc_tag)` blocks → one loop over a `(name, probe)` tuple |
| `vesselmark.py` | 645 | SIMPLIFY | one production caller, `cli_commands/guide.py:184`; the GUI references nothing in it (`rg -i vessel gui/` finds only two CSS comments). Cut: `wave` 287, `_phase_offset` 275, `_amplitude` 314, `pulse_frame` 328, `_Synchronised`, `_single_keystrokes` 602, `_key_pressed` 621. Keep `mark_levels`, `render`, `beside`, `can_draw`, `show_greeting`, and `frame_levels`+`_entrance_offsets` if the fade-in stays |
| `hashutil.py` | 46 | KEEP | the survivor; **zero direct tests** (`rg hash_and_size tests/` = 0) |
| `identity.py` | 255 | KEEP | the sole implementation; no fork anywhere in src |
| `timeutil.py` | 231 | KEEP | the survivor for tabular dates; FHIR ISO forks fold in |
| `commands.py`, `upload_command.py`, `profiles.py`, `runmanifest.py`, `migration_status.py` | 603 / 522 / 546 / 434 / 153 | KEEP, move to `commands/` | one flow each, two frontends; `run_upload_command` (343, 180 lines) splits per §5 |
| `atomic.py`, `locking.py`, `logutil.py`, `output.py`, `textutil.py`, `ccda_codes.py`, `codes.py`, `conservation.py`, `model_paths.py`, `outcome.py`, `presentation.py`, `vesselmark_data.py` | — | KEEP | one implementation each; every module docstring over the cap |

### 4.5 deliver/browser and deliver/verify

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `browser/__init__.py` | 116 | SIMPLIFY | eager re-export of 37 names; `rg "from anastomosis\.deliver\.browser import"` across src and tests = 0 hits. Every consumer imports the submodule. Becomes a docstring-only marker |
| `browser/fake.py` | 260 | KEEP, relocate later | a test double in src with six test callers and a stated future `--dry-run` use; `FakeDestination.__init__` has eleven knobs |
| `browser/persist.py` | 763 | KEEP | `load_upload_manifest` (645) CC 17, the highest in the package; `write_upload_manifest` (373) has a 38-line docstring narrating #374 |
| `browser/engine.py` | 449 | KEEP | `run` 86 lines CC 13; `_lifecycle` 72 |
| `browser/attach.py`, `fhir_api/attach.py` | 59 / 84 | KEEP | the seams themselves; the three wrappers over them go (§8) |
| `verify/levels.py` | 645 | SIMPLIFY | seven level classes with no constructor state; `:170 date_renderings`, `:180 _date_present` are one-line delegates duplicated in `qa/checks.py:173,183`; `PdfSnapshot` (218) and `qa/checks.py:87 _SnapshotCache` are the same cache twice |
| `verify/composite.py` | 431 | KEEP | `_POLICY_SKIPS` (106–117) is the level→policy table already |
| `verify/types.py` | 71 | KEEP | four production importers and `test_import_boundaries.py:173-230`; only the rationale docstring goes (RULES.md 54) |
| `browser/tracking.py` + `browser/persist.py` | — | KEEP both | not one store: the manifest is immutable WHAT with demographics, the ledger is mutable PROGRESS with no column typed for demographics; its `file_path` column is name-derived and never logged |

The import cost the plan attributed to `core.migrate` is real but lands elsewhere: `import anastomosis.core.migrate` is 0.027 s / 93 modules (its browser imports are already function-local); `import anastomosis.deliver.browser.persist` is 0.310 s / 364 modules because `__init__` drags in `sqlite3`, the engine, tracking, CDP and `verify.composite`. `import anastomosis.cli` is 0.084 s / 223 modules already, so S-3 may not claim a faster CLI.

### 4.6 deliver outputs

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `archive/archive.py` | 826 | MERGE target | `deliver` 160 lines; `_copy_patient_attachments` 87 and `_copy_patient_pdfs` 83 are the budget→claim→copy loop written out per call site |
| `bundle/bundle.py` | 458 | MERGE-INTO `archive.py` with `grouping=` | same write-FHIR, copy-attachments, copy-charts, name-dir, README steps; genuinely different only in the per-encounter HTML page vs the per-patient QA JSON slice, and the cross-patient index that bundle has by design not got |
| `archive/templates.py` | 240 | KEEP | already Jinja2 with autoescape, three templates, CSP constant; the plan's "three Jinja files" exists |
| `ccda_export/builder.py` | 1,860 | KEEP, extract table | nine section builders (§4.1); `_extensions_section` (51899-3) and the artifact refs are anastomosis-only and not parser inverses |
| `fhir_api/destination.py` | 673 | KEEP | `PayloadTooLarge` bound and `_same_origin_path` were policies with no rule; now RULES.md 42–43. Two broad `except Exception` at 112 and 359 are fail-closed on purpose |
| `render_index.py` | 204 | KEEP | `load` (137) 58 lines CC 14; an inline comment at 92–98 narrates history ("used to keep the last entry") and goes |
| `deliver/__init__.py`, `fhir_api/__init__.py`, `ccda_export/__init__.py` | 21 / 35 / 21 | KEEP | doc-only; their rules are now RULES.md 75 |

Neither archive nor bundle zips today; the plan's "zip" step does not exist and is not a duplicate.

### 4.7 packgen

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `emit.py` | 1,220 | SIMPLIFY | `_render_template_html` 141, `_render_draft_md` 103, `_render_ocr_evidence_file` 65, `_render_unplaced_file` 29 build markup as f-strings, escaping `{{`/`{%` to entities so generated Jinja does not collide with f-string braces (`_escape_html` 891–906, quadruple braces 755–875). Template files remove the hazard and ~300–350 lines. `_render_pack_yaml` (88) stays hand-built: it controls float format and key order for rule 35. `_comment_safe` (977) dead: CUT |
| `__init__.py` | 168 | SIMPLIFY | 47-line docstring restating the four modules; omits `emit.py`, which `core/packinit.py` imports directly around it |
| `ocr.py`, `extract.py`, `evidence.py`, `infer.py` | 629 / 445 / 671 / 783 | KEEP | the detector, 2,528 lines, no function reaches CC 15; `infer.py:14-18,78-81,385-390` retell the #200 fix three times and go |
| all six writers in `emit_draft_pack` | — | finding | plain `.write_text`, not `core/atomic`; no crash-mid-write test. Decision for S-6: drafts are hand-edited files; either route them through `atomic_write_text` or state in RULES.md that a draft may be partial |

`core/packinit.py:199 run_pack_init` and `core/source_init_command.py:193 run_source_init_command` are the same shape with one variable: frozen `*Command` with `confirmed: bool = False`, frozen `*Result` with `ok`/`error`, a pre-analysis name refusal, an unconfirmed run returning the proposal via `dataclasses.replace` and writing nothing. Neither imports the other.

### 4.8 reconstruct, packs, destinations

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `reconstruct/packs.py` | 663 | SIMPLIFY | `_load_pack_dir` (421–444) and `_load_pack_snapshot` (447–483) are same-shaped; 54-line module docstring |
| `reconstruct/provenance.py:94 _digest` | 16 | MERGE-INTO `core/hashutil` | fifth streaming sha256; the `UNREADABLE` sentinel becomes an argument |
| `reconstruct/packtrust.py:110` | — | MERGE-INTO `core/hashutil` | third |
| `reconstruct/packctx.py` | 107 | KEEP | zero static importers; loaded through the sandbox allowlist at `packexec.py:109`. **A static pass must not delete it** |
| `packs/practice_fusion_soap/context.py` | 1,089 | SIMPLIFY | `build_context` 121 lines CC 33, `build_record_context` 163 lines CC 28, `_RecordViewIndex.build` CC 20, `_payment` CC 22, `_coverage_view` CC 17; nobody calls this pack by default (`migrate.py:79` names `generic_soap`) | shared view formatters to `packctx.py`; the replica keeps its 35-section layout |
| `packs/generic_soap/context.py` | 81 | KEEP | `build_context` CC 16 in 46 flat lines |
| `destinations/loader.py` | 270 | MERGE-INTO one discovery walk | its own docstring says it mirrors `reconstruct.packs`; same three-origin order retyped |
| `destinations/browserpack.py` | 1,031 | SIMPLIFY | `_REQUIRED_SLOTS`/`_OPTIONAL_SLOTS`/`_FORM_SLOTS` (97–149) are three Python tuples duplicating the YAML keys every `pack.yaml` and `selectors.yaml` already state; the page-driving group (~600 lines) cannot be data. 44-line module docstring |
| `destinations/tebra/__init__.py` | 20 | MERGE-INTO `tebra/pack.yaml`'s header | doc-only; says what the YAML header says |
| `destinations/__init__.py` | 71 | SIMPLIFY | docstring line 5 says "land in M2", a stale phase marker |
| `reconstruct/packs.py:143 PageGeometry` vs `packgen/infer.py:537 PageGeometry` | — | finding | same name, different shape (manifest inches vs measured points), one converter between them. Rename one |

The registry: 13 entries; `tebra` alone declares `browser: {kind: pack}`; three declare `doc_write_api: fhir_documentreference` (`epic`, `canvas`, `oracle_health`), two `vendor_rest` (`athenahealth`, `drchrono`), five `unverified`, two `none`. No code fabricates a pack for the twelve: `load_destination_pack` raises naming the missing directory, `SelectorMap.from_yaml_dict` refuses a DISCOVER-only scaffold, `gui/controller.py:287-350` reports `pack: None`. The one place all thirteen look equally ready is `cli_commands/guide.py:481-486 _destination_options()`, an unfiltered `sorted(...)` of the registry; that is a presentation fix, not a registry one.

### 4.9 qa, pipeline, cli

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `qa/checks.py` | 796 | SIMPLIFY | seven checks average ~70 lines each; `UnattributedVitalsCheck` (367) has a 44-line docstring on a 21-line `run`; `_date_present`/`_date_spellings`/`_name_present` (160–183) are one-line delegates duplicated in `verify/levels.py` |
| `qa/base.py` | 113 | SIMPLIFY | `_REGISTRY` + `register_check` + `engine_checks`: populated by one loop (`checks.py:787-796`), read by one function (`base.py:110`). A module-level tuple removes the registry, the protocol and the duplicate-name check |
| `qa/wholepatient.py` | 190 | SIMPLIFY | two scope tables → one `scope=` argument; 50% prose |
| `pipeline.py` | 1,678 | SIMPLIFY | `run_pipeline` (1336) 193 lines, split at steps 3–4 (pack resolve + both guards, ~55 lines) into `_resolve_pack_and_guard`; `:1421-1426` margins dict + engine construction duplicated at `cli_commands/packsrc.py:162-167`; `_run_qa_stage` (1560) narrates #383 twice and #392 in a 79-line docstring |
| `cli_commands/upload.py:86 upload_cmd` | 274 | SIMPLIFY | 120 lines are the Typer signature and docstring; step 4 (254–326, the attach-seam construction, 70 lines, most of CC 22) splits into `_resolve_api_attach`/`_resolve_browser_attach`; the dispatcher lands under 100 lines |
| `cli_commands/migrate.py:20 _resolve_migration_profile` | 67 | SIMPLIFY | CC 17 from five explicit-overrides-saved checks, one per field; a table walked once drops it to ~6 |
| `cli_commands/guide.py` | 726 | SIMPLIFY | every prompt and label is an inline Python string; `_FLOWS` (459–465) maps keys to five `_flow_*` functions; a question table plus one runner |
| `cli_commands/packsrc.py:27 _synthetic_preview_record` | 97 | KEEP, finding | a hardcoded synthetic `PatientRecord` literal with zero coverage; move it to a fixture the preview and a test both read |
| `cli_commands/_paths.py:55 out_file` | 3 | CUT | coverage-zero, one statement, no caller that reaches it |
| `cli.py:58,71 _make_destination/_make_fhir_destination`, `:571 _make_validator` | 4 / 15 / 22 | CUT after S-13 | monkeypatch seams; see §8 |
| `cli_commands/_options.py`, `delivery.py`, `_paths.py` | 73 / 112 / 68 | KEEP | each is already a SIMPLIFY fix for a prior duplication |

The QA comparison the plan leaned on is wrong in the direction it assumed: Tebra's QA is 1,684 lines over ten check files and a 238-line runner; `vitals.py` alone is the 82. Anastomosis `qa/` is 1,286. The slice's case is the registry, the duplicated delegates and the doubled snapshot cache, not size.

### 4.10 gui

| path | lines | verdict | reason / what falls into place |
|---|---|---|---|
| `gui/consoles/runs.py` | 714 | SIMPLIFY | `_run_pipeline_locked` (285, 98 lines) and `_run_migration_locked` (511, 120 lines) share one five-step shape with different commands and result types |
| `gui/controller.py` | 601 | KEEP | `GuiApi` exposes **19** methods (`controller.py:545-601`, recounted); the dead-code report said 14 and did not reproduce. Every method has a JS caller and every `pywebview.api.*` call names a method; `test_bridge_surface.py` pins the join |
| `gui/web/shell.js` | 1,881 | KEEP, SIMPLIFY | `wireChooser` (754, 197 lines, ~71 branches) and `initSegmentToggles` (539, 163 lines, ~53) are a WAI-ARIA combobox and a pointer-drag pill; `buildRunForm` (1272, 206 lines) is a DOM builder. `countsText` (221–239) is byte-identical to `app.js:129-147`, and its own comment says so |
| `gui/web/app.js` | 460 | SIMPLIFY | `pendingRun/askForRun/abandonRun/beginRun` (200–236) identical to `wizard.js:242-263` |
| `gui/web/wizard.js` | 457 | KEEP, SIMPLIFY | **the Migrate run view, not a teach wizard.** Its bulk is `ROUTE_NAME`/`ROUTE_WHAT`/`renderGuidance` prose tables. The plan's S-7 premise that it is a third wizard is wrong |
| `gui/web/packgen.js` | 224 | SIMPLIFY | `setAnalyzing` (86–103) and `fetchResult` (76–84) identical to `source.js:1002-1019, 989-997` modulo one element id and one API name |
| `gui/web/source.js` | 1,138 | SIMPLIFY | shares the scaffold; its proposal is a stateful editable mapping table (nine state variables, fifteen helpers) and is genuinely different. That part stays |
| `gui/shell.py:119 _webview2_user_data_folder` | 52 | KEEP | 30 of 52 lines are the one docstring in the GUI that earns its length: an undocumented pywebview/WebView2 interaction no other file states |

The five-module import cycle the dead-code pass found (`gui.consoles.upload ↔ gui.consoles ↔ core.selfcheck ↔ gui.controller ↔ gui.shell`) has three eager edges and three function-body edges, so it never fails at import today. The one edge that crosses the intended layering is `core/selfcheck.py:138,164` importing `gui.shell._WEB_DIR`; S-2a moves `selfcheck` out of `core` and the cycle is gone.

## 5. Functions that earned a row

Complexity from `radon cc -s`; lengths from `ast`. Only C-rank and above, or over 100 lines, are listed here; the reports list every function over 40.

| function | lines | CC | verdict |
|---|---|---|---|
| `sources/pf_tebra/mapper.py:1670 map_export` | 216 | 36 | table the nine one-row mappers it calls; the accounting, grouping and derived values stay |
| `sources/fhir_r4/mapper.py:1468 _assemble` | 119 | 36 | vendor control flow; not a lift |
| `sources/fhir_r4/mapper.py:1348 records_from_resources` | 118 | 33 | partition logic; not a lift |
| `sources/fhir_r4/mapper.py:514 _patient` | 90 | 32 | consumed-path bookkeeping inflates it; consumes the S-4 table's paths |
| `packs/practice_fusion_soap/context.py:745 build_context` | 121 | 33 | shared formatters out to `packctx` |
| `core/fhir/export.py:180 _patient` | 111 | 30 | S-4 table |
| `packs/practice_fusion_soap/context.py:580 build_record_context` | 163 | 28 | same |
| `sources/fhir_r4/mapper.py:774 _observations` | 134 | 27 | BP-panel expansion; stays |
| `core/fhir/ingest.py:494 from_bundle` | 62 | 25 | S-4 table |
| `cli_commands/upload.py:86 upload_cmd` | 274 | 22 | split at step 4 |
| `packs/practice_fusion_soap/context.py:254 _payment` | 26 | 22 | guarantor branching; table |
| `sources/fhir_r4/mapper.py:1093 _coverage` | 55 | 21 | class-array tiers; stays |
| `packs/practice_fusion_soap/context.py:387 _RecordViewIndex.build` | 68 | 20 | indexes nine collections; stays |
| `sources/pf_tebra/mapper.py:253 _map_patient` | 62 | 19 | one lift + five side-table joins; table the lift |
| `sources/ccda/parser.py:2578 parse_document` | 90 | 17 | the LOINC dispatch ladder; the S-9 table |
| `cli_commands/migrate.py:20 _resolve_migration_profile` | 67 | 17 | table of five fields |
| `deliver/browser/persist.py:645 load_upload_manifest` | 107 | 17 | versions 1–4; keep |
| `packs/practice_fusion_soap/context.py:1046 _build_flowsheet`, `:214 _coverage_view` | 44 / 23 | 17 / 17 | formatters to `packctx` |
| `packs/generic_soap/context.py:36 build_context` | 46 | 16 | flat; keep |
| `sources/oracle_ehi/mapper.py:575 map_export` | 70 | 16 | orchestration; keep |
| `sources/fhir_r4/mapper.py:652 _facility` | 47 | 15 | Location vs Organization; keep |
| `core/fhir/export.py:831 to_bundle` | 41 | 15 | one line per entity; S-4 |
| `pipeline.py:1336 run_pipeline` | 193 | 13 | split at steps 3–4 |
| `core/upload_command.py:343 run_upload_command` | 180 | 9 | split steps 4–5 (resource wiring, ~40 lines) into `_wire_run_resources` |
| `core/migrate.py:609 _run_ccda_standard` | 127 | 9 | split |
| `deliver/archive/archive.py:221 ArchiveDeliverer.deliver` | 160 | 7 | S-5 |
| `gui/consoles/runs.py:511 _run_migration_locked` | 120 | — | one locked runner with `_run_pipeline_locked` |
| `packgen/emit.py:735 _render_template_html` | 141 | 6 | template file |
| `packgen/emit.py:1070 _render_draft_md` | 103 | 6 | template file |

## 6. Merge maps per slice, with the estimate the evidence supports

Each slice's PR cites these rows. Where the evidence supports less than the plan estimated, the smaller number is here and the reason is stated. The plan's totals are in §14.

- **S-1 prose sweep.** Every module docstring in `src/` exceeds 10 lines (range 12–73). Function docstrings over 5 lines: 124 in `sources/ccda`, 72 in the other sources, 105 in `deliver/browser`+`verify`, ~90 in `qa`+`pipeline`+`cli`, 76 in `reconstruct`+`packs`+`destinations`, 51 in `packgen`, and more. The rules those docstrings protect are now RULES.md 2, 5–11, 16–18, 28–32, 37, 41–56, 59, 61, 67–68, 71–76 (§12). `tools/ast_equal.py` proves nothing but prose moved. Estimate −9,000 to −12,000 lines of prose. Firm.
- **S-2 shared primitives.** Five chunked digesters → `core/hashutil.hash_and_size` with an `unreadable=` argument: `core/profiles.py:104`, `sources/pf_tebra/mapper.py:1557`, `reconstruct/provenance.py:94`, `reconstruct/packtrust.py:110`. Two atomic forks → `core/atomic`: `core/migrate.py:810`, `core/sourcelearn.py:682`. FHIR ISO date forks → `core/timeutil` with a `pad_partial=` argument: `core/fhir/export.py:173`, `ingest.py:88,92`, `sources/fhir_r4/mapper.py:363,379`. Four delegates deleted: `qa/checks.py:160,173,183`, `deliver/verify/levels.py:170,180`. One snapshot cache: `verify/levels.py:218 PdfSnapshot` and `qa/checks.py:87 _SnapshotCache`. Add the missing direct test for `hashutil`. Estimate −400 code.
- **S-2a command layer out of core.** Move `commands`, `migrate`, `migration_status`, `packinit`, `profiles`, `selfcheck`, `source_init_command`, `sourcelearn`, `upload_command` to `commands/`; only import lines change; `grimp` must show `core` with zero outward edges; `core/selfcheck`'s import of `gui.shell._WEB_DIR` becomes a `commands`-level import and the GUI cycle closes. Line-neutral. Structural.
- **S-3 import graph.** `deliver/browser/__init__.py` becomes a docstring-only marker (zero external users of its re-exports). `verify/types.py` stays. `core/migrate` and `core/commands` already import `.persist` directly. Estimate −100; the benefit is `persist` importing without the engine, not CLI startup.
- **S-4 one FHIR layer.** One per-resource field table from the 79-row inventory; `to_bundle` and `from_bundle` walk it; `sources/fhir_r4/mapper.py` consumes the table's path constants and keeps its residual walker. Ten things stay as code beside the table: the two-entity Patient resource, the three-way `_actor` dispatch, the double-shipped note, attachment metadata from the deliverer, `_entries`' refusal, the `_urn`/`_ref`/`_unref` codec, `_pref`, the RelatedPerson relationship fallback, the constant `MedicationRequest.status`, and `_location`'s vestigial argument. Fix the four asymmetries or write them down. Estimate −700 to −1,000 code, not −2,600. Highest risk; `test_fhir_live` against the HAPI container is mandatory.
- **S-5 archive, bundle, PF pack.** `BundleDeliverer` (`bundle.py:108`, 320 lines) folds into `ArchiveDeliverer` with `grouping=`; the budget→claim→copy loops (`archive.py:400,488`, `bundle.py:289,316`) become one; README templating becomes one mechanism. The PF pack's `_fmt_date_short`, `_coverage_view`, `_demographics`, `_immunization_view` and siblings move to `reconstruct/packctx.py`. Estimate −500 to −700, not −1,400: `templates.py` was already right and nothing zips.
- **S-6 one learn capability, Python.** `commands/learn.py`: `LearnCommand(kind=tabular|layout, confirmed=False)`, `LearnResult`, one name-refusal against one registry argument, one confirm gate (already byte-identical), one trust write. Genuinely different and kept behind the `kind`: input resolution (one file vs N samples), the format flow's per-item review step, the write shape (three atomic files vs six draft files), the trust store (`source_trust.json` vs `PackTrust`), the format flow's in-process `register()`, the layout flow's `_discard_draft`. `packgen/emit.py`'s HTML and Markdown builders become template files. `pack init` and `source init` stay as Typer aliases in `packsrc.py`. Decide the draft-atomicity question (§4.7). Estimate −900 to −1,200, not −2,400.
- **S-7 one learn capability, JS.** `wizard.js` is out of scope (Migrate view). Share `setAnalyzing`/`fetchResult`/the `onAnalyze` wrapper between `packgen.js` and `source.js` through one descriptor (stage name, input ids, the three API names, a `renderProposal` callback); share `countsText` and the `pendingRun` idiom through `shell.js`. `source.js`'s mapping table stays. Estimate −250 to −350 JS, not −900.
- **S-8 vesselmark.** As §4.4. The owner sees the before/after animation before merge. Estimate −450 src, −600 test.
- **S-9 C-CDA descriptor table.** One `SectionSpec` row per section drives `parse_document`'s dispatch and `build_ccd`'s call sequence; bodies untouched. Estimate −300 code; the corpus pin must not move. The package's real reduction is S-1's prose (42.6% of 4,983).
- **S-10 tabular sources.** The nine pure-table entity mappers in `pf_tebra` and the two in `oracle_ehi` take `learned/spec.py`'s `FieldMapping` shape; joins, refusals, `_PlanTypeLookup`, the CLINICAL_EVENT classifier and the two-path document artifact stay code. Estimate −400 to −600, not −1,500.
- **S-11 QA and verify.** `_REGISTRY` → tuple; `wholepatient` scope tables → `scope=`; seven level classes → functions behind `_POLICY_SKIPS`; the delegates and the doubled cache went in S-2. Estimate −200 to −300, not −700.
- **S-12 destinations.** One discovery walk shared by `reconstruct/packs.py` and `destinations/loader.py`; `_load_pack_dir`/`_load_pack_snapshot` → one; the three slot tuples generated from one schema; `tebra/__init__.py` → the YAML header; `guide.py`'s destination picker shows pack readiness. Estimate −300 to −400, not −800.
- **S-13 command layer and seams.** `attach=` on `run_upload_command` replaces `cli.py:58,71` and `gui/controller.py:79`; `upload_cmd`, `run_upload_command`, `run_pipeline`, `_run_ccda_standard` split at the steps in §5; `guide.py` prompts → a table; `selfcheck.py` → a loop; `_resolve_migration_profile` → a table; `runs.py` → one locked runner; the pipeline/packsrc engine-construction duplicate → one function. Estimate −700 to −900.

## 7. Duplicates confirmed, pair by pair

| pair | one variable |
|---|---|
| five streaming sha256 readers (S-2) | unreadable → raise / `None` / `UNREADABLE` |
| `core/migrate.py:810`, `core/sourcelearn.py:682` vs `core/atomic.py` | `mode=`, `newline=` |
| `core/fhir/export.py:173`, `ingest.py:88,92`, `fhir_r4/mapper.py:363,379` vs `core/timeutil` | partial-date padding |
| `qa/checks.py:160,173,183` vs `deliver/verify/levels.py:117,170,180` | none; delete the wrappers |
| `qa/checks.py:87 _SnapshotCache` vs `verify/levels.py:218 PdfSnapshot` | per-document vs per-item |
| `cli.py:58 _make_destination` ≡ `gui/controller.py:79 _attach_destination` | none; byte-identical |
| `core/packinit.py:199 run_pack_init` ≡ `core/source_init_command.py:193 run_source_init_command` | `kind` |
| `reconstruct/packs.py:504-583` discovery ≡ `destinations/loader.py:162-270` | the per-origin loader |
| `reconstruct/packs.py:421 _load_pack_dir` ≡ `:447 _load_pack_snapshot` | source of bytes |
| `gui/consoles/runs.py:285 _run_pipeline_locked` ≡ `:511 _run_migration_locked` | the command and its result type |
| `pipeline.py:1421-1426` ≡ `cli_commands/packsrc.py:162-167` | `section_overrides` |
| `cli_commands/destination.py:296-300` ≡ `cli_commands/upload.py:311-315` | none |
| `app.js:129-147 countsText` ≡ `shell.js:221-239` | none; the code admits it |
| `app.js:200-236 pendingRun…` ≡ `wizard.js:242-263` | the rollback target node |
| `packgen.js:86-103 setAnalyzing` ≡ `source.js:1002-1019`; `:76-84 fetchResult` ≡ `:989-997` | element id, API name |
| `core/selfcheck.py` nine `try/except` blocks | the probe |
| nine `pf_tebra` one-row mappers | the column list |

Not duplicates, left apart on purpose: the three grouping helpers (`_rowutil.group_by`, `fhir_r4`'s multi-key partition, `learned/interpreter._group_by_patient`) solve three different constraints; `_ts_date` parser/builder is an inverse pair; `core/timeutil.parse_dt` (vendor free-text) and the FHIR ISO converters are different input shapes but the ISO ones still fold in as an argument.

## 8. Abstractions with one caller or one implementer

`Renderer` Protocol (`reconstruct/engine.py:65`; one implementer `chromium.py:43`) · `CandidateScorer` (`core/sourcelearn.py:289`; one implementer) · `SelectorValidator` (`destinations/wizard.py:79`; one implementer, one caller) · the `QACheck` registry (`qa/base.py:100`) · `cli._make_destination`, `cli._make_fhir_destination`, `gui/controller._attach_destination`, `cli._make_validator` (monkeypatch seams) · the four date/name delegates · `vesselmark` (one caller, `guide.py:184`). **Not candidates:** `Verifier` (two implementers); `reconstruct/packctx.py` (zero static importers, loaded through the sandbox allowlist).

## 9. Branches without a receipt

The full lists are in the package reports (8 in `sources/ccda`, 8 in the tabular sources, 13 in `core/fhir`, 15 in `core`, 15 in `deliver/browser`, 15 in deliver outputs, 7 in `packgen`, 8 in `reconstruct`/`destinations`, 15 in `qa`/`pipeline`/`cli`, 15 in `gui`). Three packages are well-cited enough that fewer than fifteen qualified and the auditors said so rather than pad. The ones that cost the most lines and guard the least evidenced input:

- `output.py:199-258` SDDL parsing for conditional and resource-attribute ACEs no Windows report has produced.
- `textutil.py:315-337` `<svg>`/`<math>`/`<applet>` inside a SOAP note; no PF sample has emitted these.
- `sourcelearn.py:91-92` zip-archive refusal by suffix and by magic bytes for an input nobody has pointed the tool at.
- `identity.py:149-179` wholly-CJK name joining with no fixture (kept: it is now RULES.md 6, which needs a fixture).
- `fhir_r4/mapper.py:1124-1128` `order: 0` on a FHIR `positiveInt`.
- `pf_tebra/mapper.py:920-921` a fourth-tier insurance benefit order with no fixture.
- `engine.py:289-295` a branch its own comment calls unreachable by construction, kept for "a future allocator bug".
- `ocr.py:172-174` rejecting `allow_network=True`: drop the field rather than guard it (RULES.md rule 6 shape).
- `packinit.py:179-185`, `source_init_command.py:307` `isinstance(name, str)` guards against a caller ignoring type hints.
- `cli.py:531` slicing an error message by the length of a hardcoded prefix.

Each goes with its test in the slice that touches its file, or gets a fixture and a receipt.

## 10. Swallowed exceptions worth a second look

Most `except` clauses in the tree are a documented fail-closed or a documented diagnosis contract. These are the ones where a real fault would look like nothing happened:

| file:line | what a reader would miss |
|---|---|
| `core/sourcelearn.py:317 _load_synonyms` | a broken `synonyms.json` silently degrades fuzzy matching to name similarity only |
| `core/migrate.py:777-780 MigrationProfiles.__init__` | a corrupt profile store silently starts empty |
| `core/output.py:174,279,342` | Windows SID/ACL probes return `None`/`False` with no log |
| `pipeline.py:876,916` | an unreadable settings or provenance file is treated as absent |
| `core/fhir/export.py:392` | a non-numeric `Observation.value` falls through to `valueString` with no log and no test |
| `deliver/ccda_export/builder.py:716` | a bad preserved-entry XML returns `None` with no log (PHI rationale given) |
| `packs/practice_fusion_soap/context.py:207 _fmt_copay` | returns the raw string unformatted |
| `pf_tebra/mapper.py:1600 _page_count` | `except Exception` wider than the corrupt-PDF case the docstring cites |

## 11. Findings that are defects or gaps, not refactor work

1. **The GUI's teach-a-format screen has no destination control.** `source_init`/`source_init_async` take a `destination` parameter and the console's docstring calls it "destination-before-teaching"; `source.js:1040-1074` sends `null`; `index.html` has no such control outside the Migrate view. RULES.md 32 says the destination is resolved before analysis. The CLI can; the GUI cannot. An unwired surface (potemkin-check pattern 4). File it.
2. **`packgen` writes its six draft files non-atomically.** Decide in S-6 (§4.7).
3. **`core/hashutil.py` has no direct test.** Add one in S-2, before the four forks fold into it.
4. **The dead-code report's `GuiApi` count was 14; the class has 19.** Recorded so the discrepancy is not repeated.
5. **`packgen/emit.py:522 _conflict_rows` and `core/textutil.py:531,542,546` (`handle_charref`, `handle_decl`, `handle_pi`)** have zero coverage and are real dispatch paths; the sanitizer one appends text in a PHI-facing path. Fixtures, not deletion.
6. **`cli_commands/guide.py:481-486`** lists all thirteen destinations as equally ready.
7. **`reconstruct/packs.py:143` and `packgen/infer.py:537` both define `PageGeometry`** with different shapes.
8. **`gui/web/*.js` carries thirteen "used to" comments** in `shell.js` alone; the prose gate's history words apply to JS comments too.
9. **Rule 2 is violated at eight sites.** `str(exc)` or an exception message is interpolated into a user-facing or logged message at `pipeline.py:365,368,1236,1377,1463,1470`, `core/migrate.py:182`, `core/source_init_command.py:243`; `pipeline.py:368` interpolates an adapter exception into a message the operator sees. Each becomes `exc_tag(exc)` in the slice that touches its file, or S-2 takes them all.
10. **`sources/learned/transforms.py:52,65` parse dates with `strptime`** outside `core/timeutil.py`: a fifth date-parsing fork for S-2's list.
11. **`qa/` has no `Conservation` check**: the one stage that reconciles nothing against what it was offered. S-11 adds it.
12. **Learning a new destination is manual.** The tool learns a new input format and a new page layout with the operator confirming; a new portal is still `destination init`, a selector wizard writing YAML, one shipped pack. The owner's mission statement makes a new portal the same kind of thing as a new note layout: something the tool learns and then drives, under rules 12, 13, 69 and 70. No slice designs it yet; the clean-room architect's deliverable A and S-12 are where it is decided.
13. **No committed fixture can make QA say anything but pass.** The default pack, `packs/generic_soap/pack.yaml`, declares none of the five `CHARTABLE_KINDS` in its coverage block, so `RecordCoverageCheck` can never report `not_carried` on it, and no `--section` or `--include` combination over the five fixtures produces a warn or fail. The snapshot net therefore cannot see a QA verdict regression; the six mutation tests in `tests/unit/test_qa.py` are the only guard, and they stay. S-11 adds a fixture-and-pack pair that fails a check on purpose.
14. **The raw Kareo export root yields one patient; the curated two-document pair yields two.** Driven on `main` 7738b26 and on this branch with identical results (exit 0 both; 1 rendered vs 2 rendered). The root holds two top-level XML documents of 23,311 and 29,610 bytes; whether the second is folded into the first as the same patient (rule 9) or skipped is not established. UNVERIFIED until the ledger's disposition for the second document is read.
15. **A learned mapping and a template pack answer one question two ways.** A pack whose content hash changed is unavailable until re-trusted (rule 22); a learned mapping whose `source_trust.json` hash mismatches after review only warns and still loads (`sources/learned/__init__.py`). Rule 87 says one answer. Decide in S-6 with the learn capability.

## 12. Rules the prose was protecting

Absorbed into `docs/RULES.md` before S-1 deletes the docstrings that held them. Source file:line → rule number.

| source | now |
|---|---|
| `core/logutil.py:1` exc_tag never str(exc) | 2 |
| `qa/base.py:8`, `qa/runner.py:2` QA report may quote values | 4 |
| `packgen/emit.py:912-919` raw sample text never in template.html | 5 |
| `core/identity.py:35-45` CJK flush-joined names | 6 |
| `sources/ccda/parser.py:1018` bare GUID root trusted | 7 |
| `pipeline.py:512,589` fold unions identifiers, refuses new field kinds; `parser.py:1143` encounters fold only when fields agree | 9 |
| `parser.py:2480` measurement-to-encounter link | 10 |
| `deliver/render_index.py:1` attribution by index, never filename | 11 |
| `core/hashutil.py:10` one hash definition | 16 |
| `core/textutil.py:1` one filename definition, 16 hex tag | 17 |
| `core/output.py:1` DACL read-back verification | 18 |
| `destinations/loader.py:1` same discovery order | 21 |
| `core/packinit.py:1` confirm is consent; unrecordable hash discards | 28–29 |
| `core/model_paths.py:5,23` closed set of targets | 30 |
| `core/source_init_command.py:1` refuses at a changed destination | 32 |
| `src/anastomosis/__init__.py:10` no network on the core path | 37 |
| `deliver/fhir_api/destination.py:533` same-origin server URLs; `:465` payload preflight | 42–43 |
| `deliver/browser/manifest.py:1` builder raises on missing/changed/absent | 44 |
| `deliver/browser/tracking.py:17,28` inside the hardened dir, no PHI column, synchronous=FULL | 45 |
| `deliver/browser/gates.py:1` v3+ with no gates refuses | 46 |
| `core/upload_command.py:1` verify on by default, fails closed | 47 |
| `deliver/browser/verify.py:1`, `verify/composite.py:1` Verifier contract, L4 escalation | 48 |
| `verify/composite.py:381`, `verify/levels.py:1`, `browser/reports.py:1`, `browser/errors.py:1` never mutates; detail/report never carry a value | 49 |
| `browser/engine.py:1`, `browser/manager.py:1` transient retry, single-threaded | 50 |
| `browser/states.py:1` recovery edges bypass validation | 51 |
| `browser/cdp.py:1` loopback + explicit port | 52 |
| `core/runmanifest.py:1`, `core/profiles.py:1` state machine and drift refusal | 53 |
| `verify/types.py:1` leaf module | 54 |
| `parser.py:1` every element name in the verified reference | 55 |
| `sources/ccda/__init__.py:74,107,194` CDA-ness by first event, codepoint order, position-named refusal | 56 |
| `ledger.py:1455` never over-credit | 57 |
| `parser.py:495`, `ledger.py:1550` capture before rewrite; re-hydrated copy | 59 |
| `parser.py:680` loss-narrative merge | 61 |
| `core/timeutil.py:1` year-1 sentinel, naive is UTC | 67 |
| `core/conservation.py:1` offered vs produced | 68 |
| `destinations/registry.yaml:7-19`, `deliver/router.py:1` | 69–70 |
| `cli_commands/guide.py:10-24` | 71 |
| `gui/shell.py:131,180` | 73 |
| `gui/controller.py`, `test_bridge_surface.py` | 74 |
| `deliver/__init__.py:19`, `deliver/browser/__init__.py:31` | 75 |
| architecture finding | 76 |

Docstrings that only narrate (a fix, an attempt, "used to") and protect no rule are listed per package in the reports; they go without replacement. The ten worst by length in `sources/ccda` are itemised there with a verdict each.

## 13. Tests

**The guard-count baseline is 72.** `rg -o '#[0-9]{2,4}' tests/ | sort -u` returns 77; five are hex-colour truncations (`#0000`, `#002`, `#1713`, `#701` in `test_vesselmark.py`, `#7373` in `test_pf_pack.py`) and are excluded; `#40` is a real merged PR (commit `948285b`, hash-pinned external packs) and stays. The plan's "31 distinct regression guards" counted something else. The gate asserts 72 as the floor.

**The plan's retire estimates were wrong in the direction that matters.** Hand-read, file by file:

| file | tests | plan said retire | hand-read hypothetical | what they are |
|---|---|---|---|---|
| `test_pf_tebra.py` | 49 | ~60% (~29) | 12 | malformed rows built by helpers, no fixture, no `#NNN`: an orphan table, a keyless demographics row, a row wider than its header, an invented two-patient plan-name share (node ids in the report) |
| `test_qa.py` | 57 | ~50% (~28) | 7 | six page mutations with no filed report (DOB deleted, wrong name, blank page, A4 page, vital deleted, render-day stamp) plus a record with no name and no DOB, which no adapter produces |
| `test_ccda_ledger.py` | 101 | ~25% | ~25% | hand-written spec-legal C-CDA constructs; most are receipted by the spec, a quarter guard shapes no vendor has emitted |
| `test_vesselmark.py` | 40 | 30 of 40 | 0 | deterministic geometry, luminance and animation-state checks; there is no vendor input to hypothesise about. They retire with the functions S-8 deletes, not as hypotheticals |
| `test_fhir_r4_source.py`, `test_ccda_export.py`, `test_gui_controller.py`, `test_browserpack.py`, `test_archive.py` | 60 / 89 / 101 / 52 / 24 | — | ~0 | receipted by the FHIR spec, RULES.md §8, application logic, RULES.md §2, and RULES.md 40 respectively |

A mechanical AST classifier put 2,062 of 2,513 tests in the hypothetical column; its author stated its blind spot (a test is "regression" only if a `#NNN` or golden marker appears in its body) and the hand-read numbers above are the ones that count. **The test suite is real.** Its reduction comes from four places: the tests of code the slices delete (S-8's animation, the duplicated wrappers, `emit.py`'s string builders once they are templates); the 19 hypothetical tests named above, each retired with the branch it hypothesised about; test docstrings, 429 of which exceed five lines across 92 files (`test_ccda_ledger.py` 59, `test_ccda_export.py` 35, `test_one_patient_is_one_chart.py` 28); and four output-contract tests that become snapshot assertions (`test_qa.py::test_report_json`, `test_render_provenance.py`, `test_upload_manifest_attachments.py`, `test_run_binding.py`, after the pattern `test_golden_rendering.py` already uses).

**Skips and conditions.** 0 `xfail`, 0 unconditional `skip`, 16 `skipif` (POSIX-only, Windows-only, no-OCR-engine), 144 `importorskip` gating optional extras (119 `pymupdf`, 18 `playwright`, 5 `fhir.resources`, 1 `build`). Matches the survey exactly.

**Vacuous.** Two: `test_locking.py::test_lock_released_after_block` (`:27`) and `::test_lock_acquires_a_leftover_unheld_marker` (`:36`) enter the lock twice and assert nothing; they would pass if `output_lock` were a no-op. Rewrite to assert the second acquisition's outcome. `test_browser_states.py::test_legal_transitions_accepted` (`:59`) asserts nothing alone but is paired with a test that does. No mock-asserting-on-its-own-mock found.

**Fixtures.** Every directory under `tests/fixtures/` declares synthetic (`feedface-`, 555, SSN ≥ 900) or Synthea origin in a README except `learned/clinic_visits.csv`, which carries the markers but no README: add one. `tests/reference/README.md` is stale; the file it documents now ships inside `sources/pf_tebra/`. Goldens: `tests/e2e/goldens/*.json` + `.words.json` (344 K), `tests/unit/goldens/packgen_ocr_layout.json`.

**Doubles in `src/`.** One: `deliver/browser/fake.py` (`FakeDestination`, `FakeCrash`, `_FakeSession`), used by six test files and by nothing in production. It moves to `tests/` unless the `--dry-run` its docstring promises is built.

## 14. The honest arithmetic

| | plan said | evidence supports |
|---|---|---|
| prose sweep | −8,500 | −9,000 to −12,000 |
| twelve code slices | −15,250 | −5,000 to −7,000 |
| `src/` after | ~24,900 (from 48,665) | ~33,000 to ~38,000 (from 52,070) |
| tests | −22,000 to −26,000 | −10,000 to −15,000: prose in 429 docstrings, the tests of deleted code, 19 hypotheticals, four contract tests folded into snapshots |
| `tests/` after | ~32,000 | ~50,000 (from 64,002) |

The halving in the owner's brief is metaphor for "everything it does, in as little as a veteran would write". The evidence says the veteran's version of this tool is about a third smaller in code and two-thirds smaller in prose, not half in code, because two of its subsystems are already at the size the problem costs (C-CDA, layout inference, by reference measurement), one is seven times smaller than its reference (the upload engine), and one is already smaller than its reference (QA). What remains large after the slices is large for a reason a row in this ledger names. Where a slice's structural well runs dry before its estimate, the PR says so and stops; that is the rule from `banach-tarski-refactor`, and it is why these numbers are written down before the first cut.
