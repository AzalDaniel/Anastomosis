# Changelog

All notable changes to Anastomosis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes (noted here when they happen).

## [Unreleased]

Post-0.7.0 audit work: a full pass over the shipped surfaces, driving the real
CLI and the real GUI rather than reading them, with every finding raised as an
issue and fixed in its own pull request.

### Added

- **A positive verdict has to be backed by the thing it claims.** An
  adversarial pass over the C-CDA conservation ledger found three ways it
  awarded credit it had not earned, and every one of them read as preservation
  to an operator. A participation was counted preserved when a `ccda:` key
  existed, whatever was under it — so an adapter that wrote the namespace and
  stored an empty list scored what one storing the facts scored, and the credit
  was for the cheapest thing in the record to be right about. An `<entry>` was
  counted parsed when ANY id beneath it reached the record, so an `<organizer>`
  of two results kept its verdict after one of the two was dropped, on the
  strength of its sibling. And an entry the parser could not take was counted
  preserved by its section's prose, one shared flag handed to every entry
  beneath it.

  Each is now asked at its own address. The parked payload is counted, not the
  key, and one stored item answers for one offered construct and is spent.
  An entry's clinical statements are asked one at a time and the answer is all
  of them — calibrated against the statement KINDS this document has been seen
  to link, by template OID, because a Problem Concern Act is what a condition
  is recorded by and its nested Problem Observation never carries provenance of
  its own; requiring that one would report every conforming problem as half
  lost, which is the same lie told backwards. And an unparsed entry is credited
  only by a verbatim copy of itself, or by the narrative cells it NAMES, never
  by prose about the section.

  That second route is C-CDA's own: an entry says which cell of the table is
  its human-readable form, in `<reference value="#id"/>`, and a cell whose
  words the record kept is a real preservation of that entry. It is held to
  the same arithmetic as everything else here. The cells are the named things
  strictly inside this section's `<text>` — the `<text>` itself is the whole
  prose, and a `<table>` or a `<caption>` is the arrangement, not a statement
  in it.

  A word can be addressed by more than one name. A table writes the row's
  name on the row and the cell's name on the cell inside it, and an entry may
  reach the word by either; C-CDA's ordinary spelling has one entry use both
  at once. So the names are addresses and the innermost cells are the claims:
  every name over a word leads to the one claim on it, and one entry naming
  it twice over is one preservation, not two. Each claim answers one entry
  and is spent, so three entries citing one row are one preservation and two
  losses. An entry whose citations do not ALL resolve is credited by none of
  them, because a citation naming nothing is a claim the document cannot
  back, and it keeps no half it did find — that half belongs to whichever
  entry named it on its own.

  A section settles its entries by what they ask for, never by the order it
  happens to list them in — otherwise an entry citing a whole row takes every
  cell under it and starves the entries that named those cells, and the same
  three entries over the same two words read two preserved or one depending
  on which came first. A reading nobody can reproduce from the content alone
  is not a reading. Both ends are tried, narrowest claim first and widest
  first, and whichever honours more entries is the answer; neither alone is
  enough, because a narrow claim reaching into two rows can kill both of
  them and a wide one can swallow cells its own entries had named.

  Choosing the most entries a set of cells can honour is set packing, and
  this is a heuristic over it, stated plainly rather than implied to be
  exact: measured against a brute-force maximum over 4,000 arrangements it
  never credits more than an honest assignment could — no preservation is
  invented — and on 5 of them it credits fewer, reporting loss an optimal
  assignment would not. Over-reporting loss is the safe direction for an
  instrument whose whole purpose is to be believed about absences.

  The third of those was hiding real loss rather than only mis-labelling it,
  and the corpus this repo generates says so in its own documents: a Plan of
  Treatment whose narrative reads "Continue lisinopril and recheck blood
  pressure in three months" carries an entry stating the coded value "No
  current problems", and of 281 such entries measured, not one had every fact
  it states present in the narrative crediting it. So the 6,144-document
  reading moves, and it moves by a lot — roughly half the entries under each
  unsupported section go from preserved to not credited as data (`58cbcf57…`,
  65 lines, 3,433 bytes, against `823a60b6…` before). Nothing about the parse
  changed: the same documents yield the same charts, and the instrument stopped
  flattering them.

  A fourth defect fell out of fixing the third. `_inline_narrative_references`
  fills each `<reference>` element's text in place so the structural parsers
  can read a coded entry's referenced name, and it ran BEFORE the verbatim
  entry capture — so the copy #314 preserves was a copy of the parser's tree
  rather than of the file, and the ledger's byte-exact question about it
  answered no for entries that were sitting right there. The capture happens
  first now.

  Not done here, and named rather than left implicit: a section WITH narrative
  still parks no entries, so an entry the parser cannot take there is genuinely
  not preserved. Extending the capture is a change to what every export
  carries — the builder narrates each parked key into the 51899-3 section, so
  capturing every section's entries makes the loss narrative grow by a
  generation on each round trip — and it belongs to that decision.

- **A corpus that can see what the ledger argues about.** The 6,144-document
  corpus wrote its cited narrative one way — a bare `<content>` sitting
  directly under the section's `<text>` — and every entry that cited it was
  one this adapter takes apart structurally. A parsed entry's evidence is its
  own object, so it never asks the narrative for anything. Between those two
  facts the whole narrative-credit rule was generated into 6,144 documents and
  read by nobody: forcing every citation to fail, and forcing every one to
  succeed, both left the reading byte-identical.

  It now writes the arrangements a real document has — a row named above two
  named cells, a name at more than one level over the same words, a name on
  the arrangement itself (which must not be credited), a cell that renders an
  image instead of words, and a citation that resolves to nothing — and the
  entries doing the citing are the ones with no structured home of their own.
  Thirteen mutations of the containment and claim rules were invisible to this
  corpus before; eleven move it now, including the two the arithmetic rests
  on: all-or-nothing against take-any, and refusing a citation that names a
  cell the section does not define against crediting it for the half that
  resolved. The row is the reason those two became visible — it is the one
  arrangement where a single name leads to more than one claim, and until it
  had two named cells under it every name in 6,144 documents stood over
  exactly one word.

  Six guards are still invisible from here, and are named rather than
  implied away. That a cell wrapping another keeps no claim of its own — it
  needs a wrapper with words outside the cell it wraps. That the settlement
  gains anything from its widest-first end — on every shape here the narrow
  end is never worse. That a `<text>` carrying an `ID` is the whole prose
  and not a cell, and that a `<reference>` without a `#` is not a citation —
  no document writes either. That a linked entry is not asked the narrative
  — the corpus never makes one compete with an unlinked sibling for a cell.
  And that an address is counted by the cells behind it rather than by its
  names — no arrangement forces a different settlement order. A reviewer
  measured the last four; the first two were known.

  The reading moves accordingly, and toward credit: entries that really do
  name a cell the record kept are counted as keeping it. It moves the other
  way too, and honestly — a `<renderMultiMedia>` must name an
  `<observationMedia>` the document declares, `referencedObject` being an
  IDREFS, and that image is a coded entry this adapter has nowhere to put. So
  1,514 entries a real chart carrying a scanned tracing would offer are now
  offered, and counted lost. `58cbcf57…` becomes `71392fc9…` (71 lines, 3,702
  bytes). Nothing about the ledger changed here; the instrument was pointed at
  documents it could not see, and stopped writing one it could not have
  produced.

  The corpus's own legality test could not see that either: it stopped at the
  section's `<text>` because narrative is StrucDoc rather than CDA. It reads
  the narrative now, against StrucDoc's own models — membership only where the
  schema states a choice, position where it states a sequence — and a second
  test asks the question no content model can, that every name a document
  points at is one it declares. Both fail on the shape that got past them.
- **One loss ledger answers for one section.** A 51899-3 section this
  exporter wrote is read back as prior losses rather than parked, and the
  ledger asked whether the record held the `ccda:prior_loss_narrative` key at
  all. The parser concatenates every stamped ledger it walks into that one
  key — so a re-export carries a single deduplicated appendix instead of
  nesting each generation inside the next — which means the key's existence
  answers for the construct class and not for any one construct. A second
  stamped section, buried where the walk does not reach it, read as preserved
  on the strength of the first one's key while nothing of it was in the
  record. A clean bill of health for a section that is entirely absent.

  It claims its own entries now, all of them, out of a counted pool of what
  was actually stored. Two ledgers a document legitimately carries are still
  both credited, including when two exports dropped the same field and the
  same line arrives twice; a ledger whose lines are only partly there is
  credited by none of them.

  Counting alone was not enough, because the pool cannot say who put a line
  in it. A buried ledger repeating a line the reachable one had also written
  emptied the pool first and was reported preserved, and the section that
  really had delivered every line was reported lost — which of the two got the
  credit came down to document order. So the question is asked at the store's
  actual address: that store is filled from the parser's section walk, and a
  section off the walk is asked nothing, because nothing of it is in the
  record. The one walk both this and the parked-entry rule ask is now one
  function, so the two cannot drift.

  The last of it was the two sides reading the document in different states.
  The parser resolves a `<reference value="#id"/>` in place before it stores a
  section's narrative, so the record holds the words the pointer names; the
  ledger re-read the file, saw the pointer, matched nothing, and reported a
  section that arrived whole as lost.

  The first fix for that was wrong in a way worth writing down. Capturing
  BEFORE the parser resolves anything makes the two sides agree — and they
  agree on less: a ledger line that is only a reference has no words of its
  own, so it stored as nothing at all and the carried-forward appendix lost
  it silently. That is a real deletion in the one mechanism this toolkit has
  for keeping what it cannot model, bought to fix a reporting error. The test
  covering it asserted the verdict and never the stored bytes, so it passed.

  So the resolution happens on the ledger's side instead, on a copy of the
  document made for that one question — the verbatim-entry mirror next door
  genuinely needs the untouched tree, and hydrating the shared one breaks it.
  Ordinary sections were reading the same document in the same wrong state and
  are fixed with it: a section whose prose points into itself is no longer a
  false loss. That one costs no reading change — no generated document puts a
  resolving reference in an ordinary section's `<text>`, so the corpus is
  byte-identical either way, and it is pinned by its own test rather than by
  the pin.

- **What rendered is what was reviewed, and it can be named.** A folder of
  charts could not say what produced it. `render_settings.json` recorded the
  layout's NAME, so a run into a folder whose layout had been edited since
  reported `0 rendered, 6 skipped`, exit 0, and left the operator holding pages
  built from bytes that no longer existed anywhere; the content-hash trust gate
  did not close it either, because assets sit outside the hash and re-trusting
  an edited pack is what `--trust-pack` is for. A run now publishes
  `render_provenance.json` beside the charts: the layout's name, origin, the
  same content hash the trust gate checked, a sha256 for every file under the
  pack root, and — from a recording Jinja loader — the templates the render
  ACTUALLY read. Re-running into a folder whose charts came from different
  layout bytes is refused, naming the files that moved; a template edited
  mid-batch, which would leave one folder holding charts from two layouts, is a
  loud failure rather than a record nobody could write honestly. Settings and
  provenance stay two files on purpose: one is what a person chose, the other is
  what the machine used, and folding hundreds of digests into a whole-value
  equality check would refuse a re-run into every directory that already exists.
  (#350)

- **The reviewed route and the run's gates ride in the manifest, and an
  executor refuses without them.** `anast migrate` resolves a destination route,
  renders, verifies, and stops; delivery happens later, often on another
  machine, from the upload manifest — which carried neither the route that was
  chosen nor any record that the run's gates had passed. A bundle rendered with
  `--no-qa` read exactly like a verified one. Manifest schema v3 records both
  (`route`, `gates`), and `deliver/browser/gates.py`'s `assert_deliverable`
  refuses a bundle whose recorded gates did not pass, whose reviewed route found
  no viable way in, or whose charts no longer hash to what the manifest
  recorded — before the destination is attached, so no session is ever opened
  against a live EHR for a bundle that was never going to be filed. A manifest
  too old to carry gates is warned about rather than refused: operators have
  rendered trees on disk, and stranding them would be the worse failure. Each
  schema field group is now gated on the version that introduced it rather than
  on the current version, which had been correct only while 2 was newest.
  (#350)

- **The page that is a picture.** All 53 sample PDFs this product has been
  shown — 802 pages — carried zero natively extractable words, and 52 of the
  53 were raster documents. The layout learner refused every one of them, so
  the only real sample set there is was the one set it could not read. It now
  asks: `packgen/ocr.py` is a pinned, offline Tesseract CLI worker — TSV and
  hOCR from one run and cross-checked against each other, an environment built
  from nothing so no proxy and no tessdata leak in from the shell, one page per
  process at `OMP_THREAD_LIMIT=1`, a finite pixel cap that downsamples by a
  recorded rule and a finite page deadline. `allow_network` is in the config
  schema so the manifest can state it and refuses to be set true. Nothing is
  ever downloaded.

  What comes back is EVIDENCE, and the distinction is in the data rather than
  in a docstring. `packgen/evidence.py` classifies every page as native-only,
  mixed, image-only, ambiguous or empty, and the two streams never merge: a
  span carries its provenance, a recognized one also carries the engine's
  score, and a text layer floating inside a page-covering scan is demoted to
  `native_or_synthetic` because extraction succeeding is not evidence that the
  text is right. Where the two describe the same pixels the overlap is held —
  a duplicate is dropped from the layout candidates and COUNTED, a
  disagreement keeps both and marks the page for review — and nothing picks a
  winner. Same-reading is decided on WORDS, not on substrings: `100` read over
  a text layer saying `1000` is the two streams disagreeing about a number, and
  the count an operator reads as a safety figure has to say so. Conflict records carry page, region, both boxes and the score, and no
  text at all, because a disagreement is by construction about a value.

  The emitted draft says all of it where a person will actually read it: a
  marker in the manifest description, an OCR section in `DRAFT.md` directly
  under the same-patient caveat, `[OCR]` beside every recognized string in the
  quarantine file, per-section provenance on each inferred heading, and an
  `OCR_EVIDENCE.md` carrying the page classes, the held conflicts and the
  engine manifest. Recognition recovers no face, weight or color, so the draft
  refuses to offer its sentinel font to a CSS stack and falls back to the
  documented default. Recognized geometry may suggest lines, columns, bands
  and page breaks; recognized text may not fill a clinical field, and a
  high-risk value needs an independent structured source or a person.

  Eight synthetic goldens — short, long, empty, multiline, table, attachment,
  pagination, font fallback — are drawn, rasterized so the PDF genuinely has no
  text objects, and run against the real binary, with semantic fidelity and
  visual geometry reported and gated SEPARATELY so neither can waive the other.
  Regenerate deliberately with `tools/regen_ocr_goldens.py`.

  Every fault the worker can meet is raised as its own — a pixel cap breached,
  a non-zero exit, a missing output stream, the two streams disagreeing about
  how many words they read, and the page deadline — and each passes through a
  batch keeping its type, so `analysis failed (OcrEngineError)` arrives with
  "your samples are not implicated" rather than as an unreadable file.

  With no engine on the machine the fail-closed half stands exactly as it was —
  a raster page with no text raises and no partial pack is written — and the
  refusal now names what to install and says nothing is fetched. `pack init`
  takes `allow_ocr=False` for an operator who wants a pack built only from text
  that was read. Rationale and the alternatives weighed:
  `docs/audits/learned-source/OCR_DECISION.md`.
- **A run that names the exact inputs it was prepared under, and refuses when
  any of them changed.** A migration's charts mean something only in terms of
  the source that read the export, the destination they were shaped for, and
  the layout that rendered the pages — and all three are editable underneath an
  operator between one command and the next. A learned mapping is a JSON file
  somebody can open; a template pack is a directory; a destination entry is data
  that gets re-verified. Nothing on disk recorded which versions of them a
  folder's artifacts came from, so nothing could refuse. `core/profiles.py`
  gives each one a frozen, content-addressed profile — reusing the digests that
  already existed, the mapping hash `source_trust.json` records and
  `packtrust.pack_content_hash`, rather than inventing a second definition of
  either — and `core/runmanifest.py` writes `run_manifest.json` beside
  `charts/` and `ccda/` naming all three hashes, the run's inputs (the export
  directory's path identity, never its contents), the pipeline version, and the
  run's state. Coming back to that folder recaptures the three profiles from the
  machine as it now stands: re-running the migration, uploading from it, or
  recording a delivery against a folder whose source, destination or layout
  moved refuses loudly and names WHICH one moved, with both digests.
  `--rebind` is the explicit way to say the earlier artifacts no longer stand.
  Destinations now declare a product `version` (explicitly `"unversioned"`
  where no readable version exists — an absence stated rather than implied), and
  `anast source init --to <destination>` chooses the destination BEFORE
  teaching, so a mapping taught for one system refuses to run at another and
  names both ends. The manifest carries no clock: two runs over the same inputs
  write byte-identical files, which is what makes "did anything change?" a
  comparison rather than a judgement. `prepared` -> `delivered` -> `verified`
  is now recorded state rather than only a computed verdict, and every move past
  `prepared` requires a receipt naming its evidence — `migrate` still writes
  `prepared` and nothing else, because it still executes no delivery. See
  [docs/RUN_MANIFEST.md](docs/RUN_MANIFEST.md). (#348)

- **The guided session opens on the mark, not a pipe character.** `anast`
  typed bare on a terminal drew a half-block and a word, which is what a
  product looks like before anyone has decided what it looks like. It now
  opens on the vessel mark itself, in dots, with the greeting beside it: the
  logo's own geometry — the same recursive growth `tools/make_vessel.py`
  writes `assets/icon/icon.svg` from — sampled cell by cell into
  `core/vesselmark_data.py`, so what a physician sees in the terminal and what
  they see on the taskbar are one object read at two resolutions, and a test
  re-samples the geometry on every run so the two cannot drift. The gradient is
  density and text weight climbing together, never a hue: §11 of the design
  language holds that the terminal's background belongs to whoever is running
  the tool, and it holds for the mark as much as for a sentence. The mark
  assembles from the trunk outward in under a second, once, and any keystroke
  ends it — nobody waits on an animation to answer the question underneath it.
  It stands down entirely for three readers who are not watching: a stream that
  is not a terminal (a pipe, a redirect, CI — which gets exactly the header it
  has always been given, byte for byte), a window too narrow to hold the mark
  and a legible column of text, and anyone who set `NO_COLOR`. A console that
  cannot encode the round dots gets an ASCII ramp of the same shape, through
  the fallback the status glyphs already use. `anast --help` and every named
  command import none of it.

- **A conservation ledger for C-CDA ingest, and a corpus to run it against.**
  2,103 real documents went through the adapter, every one parsed, and eleven
  canonical collections came back empty across all of them — no practitioner,
  no facility, no coverage, no document, and not one of 12,277 encounters
  carrying a note. "It parsed" had never been asked to mean anything, because
  nothing counted what the XML OFFERED, and a count of survivors reads the same
  whether the loss was zero or total. `sources/ccda/ledger.py` is the other
  count: it walks the document independently of the parser and gives every
  section, every `<entry>` and every participation exactly one disposition —
  structurally parsed, narrative preserved, unsupported, or source-empty —
  crediting a parse only on evidence (a canonical object whose provenance names
  an id the construct carries), and balancing its books through the same
  `Conservation` primitive the render and delivery seams use.
  `tools/ccda_corpus.py` generates the documents to run it on: deterministic,
  PHI-free, 6,144 shapes spanning the six C-CDA document types against every
  combination of ten structural traps, generated at test time and never
  committed. This measures; it does not fix. (#309)

### Changed

- **Third-party pack code no longer runs with the desktop user's authority.**
  An external or taught pack's `context.py` could read any file the operator
  could read, write one anywhere, open a socket, and spawn a process — at
  import, before a chart was rendered, because nobody had ever chosen otherwise.
  `reconstruct/packexec.py` makes it a decision: every non-built-in pack now
  executes against a restricted globals mapping — no `open`, `eval`, `exec`,
  `compile`, `input` or `print`, and an import allowlist covering the canonical
  model, the pack helpers, and pure-computation stdlib, so `os`, `subprocess`,
  `socket`, `pathlib` and the rest are refused BY NAME at import and surface as
  the pack's diagnosis. Built-ins are exempt: they ship in this wheel and
  already hold the application's authority. **This is not a sandbox and the
  module says so** — a restricted globals mapping in CPython is escapable by
  anyone who sets out to escape it, and the controls that carry weight against
  hostile pack code remain the consent flag and the content-hash trust gate.
  What it buys is that the accidental and the casual stop working and say why.
  An external pack that needs an asset embeds it as a `data:` URI in its
  manifest tokens — inside the bytes the trust hash covers — rather than reading
  a file beside it that nothing pins. (#350)

- **`anast upload` refuses an unverified bundle.** A behaviour change, stated
  plainly: charts rendered with `--no-qa` (or on a machine without the render
  extra the QA checks read PDFs with) now record `qa: not_run` in their manifest
  and are refused at delivery, naming both remedies. Filing a chart into the
  wrong patient is worse than not filing it, and an unverified bundle is exactly
  the case the L0–L6 ladder exists for. Already-rendered trees from before this
  release carry no gate record at all and are unaffected — they warn. (#350)

### Fixed

- **The Windows installer was rebuilt on every source merge, and the queue was
  paid for by everything waiting behind it.** A Nuitka standalone build plus
  the installer smoke test took 63 minutes of `windows-latest` when it was
  last measured, on the scarcest runner class and out of the same pool every
  pull request's test matrix draws on — and the lane watched `src/anastomosis/**`, so four merges
  in one evening bought four builds of an artifact nobody downloaded. The path
  filter now watches only what decides the frozen layout (the packaging
  scripts, the workflow itself, the dependency set); a nightly build is the
  canary for a source change that breaks only once frozen; and a release tag
  still builds unconditionally. A test pins the decision so the source path
  cannot come back unnoticed.

- **A build-backend bump nobody can accept blocked every other pip update.**
  hatchling has no Dependabot `ignore` and never will — a bare one silences
  its security updates, a scoped one never fired against a two-sided bound —
  so every release is proposed and the supply-chain guard refuses the ones
  measured to emit core-metadata 2.5. Inside the single `pip` group that
  refusal was contagious: #389 carried a ruff floor and a Nuitka pin with
  nothing wrong with them, behind a hatchling bump that could not merge, and
  one Dependabot commit lands whole or not at all. The backend is excluded
  from the group now, so it arrives in its own pull request to be closed by
  hand while the batch stays mergeable. An exclusion decides which PR an
  update lands in, never whether it is opened, so nothing is silenced. #389's
  other two updates are taken here: Nuitka 4.2, which builds the frozen
  Windows exes, and the ruff floor, which only records the version the lint
  lane already resolves (#389).
- **One patient was several charts, and only one of them arrived.** Every
  per-patient destination is keyed by `patient.id` — the C-CDA export writes
  `<patient-id>.xml`, the archive and the bundle each write one directory —
  and every writer in them is exist_ok/overwrite. The C-CDA adapter yields one
  record per DOCUMENT, because a document is the unit its conservation ledger
  has to account for, so a patient with two documents arrived as two records
  and the second landed on the first. The run reported two patients over one
  file, and a physician opening it read one visit with nothing saying the other
  had ever existed; the scanned case was worse, because the attachments travel
  on a path named per document and both of them were sitting in the delivered
  archive while `bundle.json` referred to one.

  A patient is now one record by the time anything is delivered, folded at
  `pipeline.load_records` where every adapter passes — so an adapter that
  already meets the contract is untouched, and the per-document ledger the
  C-CDA reading depends on keeps measuring documents. Collections union in
  document order and are deduplicated only where the model already says two
  objects are one: two encounters under one GUID `<id root>` fold by the rule
  that already folds them inside a single document, and nothing else, because
  a rule invented for the others would delete a real repeat prescription. An
  encounter under a vendor OID root does not reach this — the parser gives it
  one id per document — so a source that names encounters that way keeps two
  encounter objects across two documents, a clinical-identity decision left
  for its own change.
  Extensions merge with the losslessness rule that governs them everywhere
  else — equal values keep their key, a carried-forward loss ledger merges as
  one ledger, and anything else two documents state differently keeps BOTH,
  parked at the `#2` variant the parser already uses for a repeated section.
  A SINGLE-VALUED demographic that disagrees is not reconciled: two documents
  stating two birth dates under one id are a source that cannot say who this
  patient is, and the run refuses at exit 2 naming the field and the count, and
  the colliding records' positions, with the patient as a run-scoped surrogate,
  never the values. A demographic the model holds as a LIST cannot contradict
  itself, and unions like every other collection — one document listing the
  home phone where the next lists the home phone and a mobile is a patient with
  two numbers, and one repeating the social security number the other omits is
  a gap rather than a disagreement. Reading those as two people would
  have refused the ordinary export instead of the ambiguous one, which is the
  opposite of what the refusal is for. Behind the fold, the three per-patient
  claims now put the record up as the witness their name is claimed against,
  so a future regression is a loud refusal instead of a silent overwrite.
  (#375)
- **An id-less organizer component was stated once by the parser and never
  matched on export, so it doubled and stayed doubled.** A results or vitals
  organizer can carry a real `<id root extension>` while one of its component
  `<observation>`s carries only `<id nullFlavor="NI"/>` — a real vendor shape,
  the panel stamped and an analyte left with no id of its own. The parser gave
  that component `source_id=None`; the exporter's `_Preserved.own` pairs a
  structured object with its preserved twin by stated id, so `None` paired
  with nothing, and the same lab fact was emitted once structured and once
  preserved on every export — a duplicate that never resolved, because the
  pairing was matching two absences rather than two ids.

  Both a parser-only and a pairing-only fix were tried and rejected: deriving
  an id on ingest alone left the export id-less again, so the next generation
  gained another `None` and the count grew without bound (6 → 7 → 8 → 9 on a
  driven case); narrowing the pairing to direct-child ids alone turned other
  id-less constructs into new duplicates and dropped a provenance-less
  Problem outright. `core.ccda_codes.organizer_component_source_id(root,
  extension, index)` gives the fix to both sides at once: a uuid5 over the
  organizer's own id and the component's 0-based position, document-intrinsic
  so it survives a rename between export and re-ingest. The parser takes it
  as `source_id` when a component states none of its own; the builder's
  `_stated_ids` adds the identical id to what a preserved entry is taken to
  state, additively — the existing any-depth `<id root>` walk is unchanged,
  so a component that DOES carry its own id is never touched by the new
  branch. The match becomes positive (id-to-id) rather than negative
  (absence-to-absence), so the new stated set can only ever gain members the
  old one lacked, never lose one — driven over both required fixtures and a
  live document: generation counts hold flat where they used to grow, and
  every model count is unchanged. `sources/ccda/ledger.py` is untouched on
  purpose: a derived id is a uuid5, never an `<id root>` the document itself
  carries, so it cannot enter `linkable_roots` or move a `links()`
  obligation.

  "Both sides read the same organizer/component id" was a promise, not yet a
  fact: the parser read an `<id>` through `_attr` (`nullFlavor`-aware,
  whitespace-stripped) while the builder read one by raw truthiness
  (unstripped), and the parser looked only at a component's FIRST `<id>`
  child while the builder scanned every one. A padded root, a padded
  extension, or a component whose first `<id>` is `nullFlavor` with a second,
  rooted `<id>` behind it then derived one id on ingest and stated a
  different one — or none — on export: the same unbounded duplication this
  entry had just closed, reopened for four shapes. `core.ccda_codes.
  first_rooted_id(element)` is now the one reading both the organizer's and
  the component's own id go through — every `<id>` child in document order,
  `nullFlavor` skipped, `root`/`extension` stripped, first survivor wins —
  so the two sides agree on what id a component states by construction. One
  driven fixture (a component's id `nullFlavor` first, rooted second) reads
  differently under `sources/ccda/ledger.py` than it did before this
  correction: the component's real, stated id is now what the parser reads
  (previously it derived a spurious one instead, having missed the second
  `<id>`), so that id is linkable and the entry moves from
  `narrative_preserved` to `structurally_parsed` in the ledger's own
  accounting — a correction to a wrong reading, not a change in what the
  document says. (#365)

- **An entry under prose was preserved by nothing.** The C-CDA parser kept a
  section's `<entry>` elements verbatim only when that section rendered no
  text, so the same coded observation — one this adapter has no dispatch for —
  survived or was lost on nothing but whether its section happened to carry a
  sentence. The finding #364 closed says prose about a section is not a copy of
  the entries beneath it, which is exactly why the sentence could not stand in
  for them.

  What kept that limit in place was the export side, and it had to be fixed
  first. A parked key was NARRATED into the 51899-3 loss section as
  `path = value` lines holding whole XML entries; a re-ingest parked those and
  the next export narrated them again, so simply preserving every section grew
  the exported loss narrative by ~15 KB per generation, without bound
  (32,455 → 48,356 → 63,788 bytes on `feedface_ccd.xml`). So the builder now
  DELIVERS them: each preserved entry is re-emitted as a real `<entry>` in the
  section carrying its code, and a code this exporter writes no section for
  gets a carrier section rather than a refusal — a section with no structured
  emitter here is the ordinary case, and refusing the export would refuse the
  common path. The loss ledger converges again, one generation later and about
  100 bytes larger than before the change: 8,400 → 9,857 → 9,857 on
  `feedface_ccd.xml`, 11,191 → 13,573 → 13,573 on the Synthea sample, still a
  fixed point at generation five.

  Delivering an entry means not saying the same thing twice. A parked entry is
  the source's own statement of a clinical fact and the canonical object read
  out of it says the same fact in the exporter's words, so each emitter now
  skips the object whose source id a preserved entry carries and emits the
  rest as usual — an object from another adapter still gets its structured
  entry, and an object the parser could give no source id is matched by an
  entry that carries none. Emitting both would have re-ingested as two objects
  where the chart has one, and four the generation after.

  The reading moves, and it moves back to where it was before #366: the
  6,144-document corpus ledger is byte-identical to the pre-#366 pin
  (`823a60b6…`, 65 lines, 3,408 bytes, against `58cbcf57…` / 3,433 bytes
  before this change), because the 10,238 entries that pass credited entries
  under nine unparsed section codes from `unsupported` back to
  `narrative_preserved` — this time on a byte-exact copy of each entry in the
  record rather than on prose that may state nothing about it. `unsupported`
  no longer occurs anywhere in that corpus, which is the honest reading of an
  adapter that now keeps every entry it is offered.

  That is a claim worth doubting, so it was driven rather than argued: strip
  the parked copies back out of the record and the column comes straight
  back — 8 entries a document turn `unsupported` on 80 of 96 documents. The
  credit rests on the copy, and the day the parser stops keeping one the
  ledger says so. What the corpus can no longer see is the OTHER route to the
  same verdict. An entry is asked for its own bytes first, and on a document
  the parser walks it now always has them, so the narrative-citation rule —
  which cell of the table an entry names, and which name over a word is a
  claim rather than an address — is never reached: deleting that whole
  subsystem leaves the 6,144-document reading byte-identical. It survives for
  the sections the walk does not reach, and is held there by its own unit
  tests rather than by the corpus. A finer instrument superseded by a blunter
  one that happens to be right more often is still a loss of resolution, and
  it is recorded here rather than discovered later. (#365)
- **`--ccda` delivered a patient's chart with none of their documents on it.**
  A C-CDA whose entire clinical content is `nonXMLBody` artifacts — a scanned
  referral, a faxed discharge summary — parsed into canonical
  `DocumentArtifact`s and was preserved byte-for-byte in the charts and in the
  archive. The same exit-0 run's `--ccda` directory, the one an operator hands
  to the receiving EHR, held neither the artifacts nor any resolvable
  reference to them, and reparsing it produced the patient with correct
  demographics and an empty chart. That is what a physician would have opened.
  The export's own declared-loss table said the bytes needed no narrating
  because "the run writes them into the attachments directory beside the
  charts" — true of the charts, and the deliverer was never given an
  attachment directory nor wrote a sidecar, so the one destination the claim
  did not cover was the one it was written on.

  The delivery now writes every source document into the delivery directory
  and names it from the CCD: an `<observationMedia>` entry per artifact,
  carrying the artifact's canonical id as its `<id root>`, the declared media
  type on the ED's `@mediaType`, and the SHA-256 on the ED's own
  `@integrityCheck`, with an ED `<reference>` naming the file beside the
  document. That construct rather than a re-embedded `nonXMLBody`, because CDA
  R2 gives a `ClinicalDocument` exactly one `<component>`: a CCD carrying a
  `structuredBody` cannot also carry a non-XML body, and the C-CDA R2.1
  Unstructured Document template is for a whole document, not an attachment to
  one. Bytes stay out of the XML, so a repeated export → ingest → export loop
  carries no base64 at all and the document settles at generation 2 and never
  moves again.

  Loud where it cannot conserve, and before anything reports success: a
  document the run resolved but that is not in the directory the run put it in,
  one whose delivered bytes do not hash to what the record witnesses, and
  inline bytes that will not decode all stop the run at exit 1 rather than
  handing a receiving EHR a chart pointing at a file that is not there. The
  reader holds the same line from the other side — a delivered document that
  did not travel with its CCD, or that was edited after it was written, refuses
  the re-ingest instead of carrying a patient whose scan silently is not the
  one their chart means.

  One more loss came out with it. The deliverer wrote `<patient-id>.xml` per
  RECORD but a C-CDA export gives a patient one document per encounter, so a
  patient with four documents got one file — the last one — and the other
  three vanished under a green line, artifacts and all. Second and later
  records for a patient are now `<patient-id>-2.xml`, `-3.xml`, in the source's
  own order; a patient with one record keeps the name they have always had.
  Delivered filenames stay PHI-free: a document is named after its own
  pseudonymous artifact id, never after the source's filename, because a C-CDA
  export names its attachments after the patient and this is the directory most
  likely to travel. The source's own filename is not lost, only moved — it
  narrates in the loss ledger with every other field CDA has no slot for. The
  CLI and the GUI reach one implementation, so both conserve or both refuse.

  A document entry is this tool's own writing, and is no longer copied as
  though it were the source's. The C-CDA ingest parks every section's entries
  verbatim so an export can re-emit rather than narrate them, and a delivered
  document was being kept twice — read back into an artifact AND parked as a
  copy — so the next export wrote the entry again beside the copy. Four
  generations of the same chart went 7,464 → 9,990 → 12,850 → 18,892 bytes,
  the artifacts doubling each round and the doubles narrating in the loss
  ledger. It settles at generation 2 and never moves again. The typed object
  is the better copy anyway: restated with the name of the file this run
  actually delivered and that file's verified digest, rather than last
  generation's. A third party's `<observationMedia>` carries none of this
  tool's stamp, is nobody's to restate, and is preserved verbatim as before.
  (#373)
- **An upload manifest with no items, for a patient whose whole chart is
  attachments.** `--upload-manifest` serialized the rendered charts and nothing
  else. A C-CDA Unstructured Document renders no encounter — its clinical
  content is a scan, carried into `charts/attachments` as a canonical document
  artifact — so a run over one wrote `manifest: 0 item(s)`, `0 patients`, and
  exited 0 while both of that patient's documents sat on disk beside the file
  that said there was nothing to deliver. The archive and the bundle carried
  them in the same run; only the upload route reported nothing, successfully.

  Every carried source document is now an item: one per delivered FILE, hashed
  and sized off the bytes an upload would actually send, refused if it no
  longer matches what the record recorded for it, and attributed to exactly one
  patient — two records claiming one delivered file is a refusal, because
  `_carry_attachments` catches that collision only while the two artifacts
  differ, and the pair that slips past it is the pair that would file one
  patient's scan into another's chart. Two items that would share an `item_key`
  are refused for the neighbouring reason: the upload ledger keys on it, so a
  collision is not an overwrite but a file that is silently never sent. The
  patient reaches `patients` by the rule every other patient always has: an
  item names them. `file_path` is
  stored relative to the bundle (`attachments/…`) instead of as a bare
  basename, so the item resolves on the machine that reads the manifest rather
  than to a file that is not there, and a stored path that would climb out of
  the bundle is refused on read.

  Each item also carries the verification policy its bytes can support, which
  is a schema v4 file. The L0–L6 ladder is calibrated for a chart this toolkit
  printed: L1 rejects a sub-KiB file because a Chromium print is never that
  small, and L2/L3 read a name, a DOB and the pack's header fields off page
  one. A scanned referral is none of those, so the levels that cannot honestly
  run over it SKIP and name that reason in the run report, while L0 re-hashes
  the bytes against the digest the SOURCE recorded and L1 still checks the exact
  page count of anything the source DECLARED pageable — declared, never sniffed.
  A pre-v4 manifest reads exactly as it did: every item in one is a chart.

  What a bundle cannot deliver is now said rather than omitted. A document a
  record names with no file in the bundle — `migrate --render ccda-standard`
  carries no attachments — is counted and warned about, loudly, instead of
  vanishing; and the run's `manifest:` line counts what the WRITER wrote, not
  the documents handed to it, because a rail reporting the input while the file
  holds something else is how this read as a clean run in the first place. CLI
  and GUI reach the one writer through the same command, and a test drives both
  over the same scanned export to keep it that way. (#374)
- **A chart with no encounters was never verified.** `run_pipeline` gated the
  whole QA stage on `if qa and result.documents:` — the per-encounter render's
  own output. A C-CDA Unstructured Document renders no encounter at all (its
  clinical content is a scan, not a coded section), so an attachment-only
  export offered nothing to that population and QA never ran: no
  `qa_report.json`, the manifest gate read `not_run`, and `assert_deliverable`
  refused a bundle nothing had ever graded — silently, since nothing in the
  stage rail said QA had been skipped.

  The bundle already carries something QA can honestly verify: the
  whole-patient record summary every pack-mode run writes, HL7's own
  stylesheet over the whole record with every chartable kind declared carried,
  so a fact family the record holds and this page does not show is a FAIL
  rather than a layout choice. The QA stage now enters whenever QA was asked
  for, full stop — a chart with no encounters still owes an operator a
  verified bundle through the summaries, and the stage's own rule (not a
  precondition on its caller) is what downgrades a run that genuinely graded
  nothing to `not_run` with a skip event, the same shape a missing PyMuPDF
  install already gets, rather than a false `pass`.

  `_render_record_summaries` returns its render result instead of discarding
  it, so the stage grades the paths it actually wrote and the record BEHIND
  each one — but two `PatientRecord`s sharing one patient id (the C-CDA
  adapter yields one per source document) render to the SAME summary path,
  and the render's own idempotent skip means only ONE of them actually wrote
  the bytes there. Deduping on the path is not enough on its own: the first
  attempt kept whichever record's render ran LAST, which under the run's own
  default (`force=False`) is the one that took the skip branch, never the
  writer — so the graded row could be checked against a DIFFERENT record's
  identity and content than the page in front of it actually shows, disarming
  `record_coverage` and failing `data_integrity` on values that were never
  going to be on that page. The render now keeps the path:record association
  live as it writes — a write always claims its path, a skip only holds it if
  nothing already has — so the association is always the WRITER. Both QA
  stages that grade a whole-patient view carried the same "re-derive the path
  per record" defect (`pipeline.py`'s pack-mode stage and `core/migrate.py`'s
  ccda-standard-mode stage) and both now read that one resolution rather than
  computing their own. The GUI reaches the identical fix for free, through the
  same shared pipeline core. (#383)
- **A `.ccd` export was never read.** The C-CDA adapter's directory walk
  matched `path.glob("*.xml")`, twice — once for the count, once for the
  load — so a document a vendor wrote under any other spelling was never
  opened, never counted, and never mentioned. Kareo/Tebra write a CCD as
  `<name>.ccd`; other vendors write `.ccda`. Driving the owner's own Kareo
  export end to end found a CCD saved beside a Summary of Care saved as
  `.xml`: the run reported `1 rendered, 0 skipped, 0 failed` and exited 0 for
  an export holding two documents, with no row anywhere for the second one —
  whole-document loss behind a green line, one directory listing before the
  conservation machinery that exists to catch exactly this ever runs. The
  adapter now matches `.xml`, `.ccd`, and `.ccda` on `Path.suffix.lower()`
  (case-insensitively, on a case-sensitive filesystem too), and `detect`
  recognises an export holding only `.ccd` documents rather than missing it
  for auto-detection. The same directory walk that finds the documents now
  also counts every OTHER file whose document element reads as CDA's
  `ClinicalDocument` but whose extension names none of the three — decided
  by the file's first start tag, not a byte window, so a leading comment,
  BOM or DTD cannot hide a real document from either count — never its
  name, which a C-CDA export gives after the patient — and that count rides
  the existing source-ledger settlement into `loss_ledger.json` and the
  run's reading beside everything that WAS opened, on the same
  reset-and-`getattr` contract the document ledgers already use; a file
  that legitimately isn't CDA at all (a `nonXMLBody`'s own referenced
  attachment, say) is not counted, because
  burying the one loss that matters in files this adapter was never going to
  read regardless would be the same false accounting by another route. The
  6,144-document corpus pin is unmoved — `skipped_files` defaults to 0 and
  rides the report only as a key nothing in the printed gap table reads, so
  the aggregate is byte-identical either way (#384).

- **The corpus generator wrote four shapes C-CDA R2.1 does not play.** The
  document's information recipient was emitted as
  `intendedRecipient/assignedPerson`, but an `intendedRecipient` plays an
  `informationRecipient`; the custodian sat before `dataEnterer` rather than
  in the sequence position `ClinicalDocument` fixes for it; a coded value put
  its `translation` ahead of the source's own `originalText`; and a
  medication's `doseQuantity` came before its `routeCode`. None of the four
  was caught by a check — the first was found by reading the specification
  while implementing participation extraction (#327), and the other three by
  the sweep that issue asked for. A corpus that emits a shape no conforming
  vendor produces is testing our tolerance rather than our conformance, so a
  new assertion now walks a generated document and refuses any element
  standing where R2.1 does not allow it: the next divergence is a failing
  test rather than a second spec-reading accident. The parser keeps reading
  the non-standard recipient — stated in a comment as vendor tolerance, with
  a fixture whose own name says it is divergence, because exporters that
  reuse their `assignedEntity` writer really do emit it and this adapter's
  posture is to read what exists. Measured, not assumed: the ledger's
  6,144-document reading is byte-identical either way (65 lines, 3,408 bytes,
  `823a60b6…`), because the ledger counts the outer participation and the
  parser credits both spellings — the shapes changed, the accounting did not.

- **The layout you taught it is now the layout you can run.** Teaching a
  document layout wrote a valid draft, said so, and left the operator on a
  screen that could not offer it: the draft went to a relative `packs/`
  resolved against whatever directory the app was launched from, and discovery
  never looked there — nor, had it looked, would it have executed a
  `context.py` it could not vouch for. So Teach reported success and the next
  run rendered somebody's charts through a different layout. Drafts now land in
  `~/.anastomosis/packs`, the per-user home the trust store, learned source
  mappings and migration profiles already share, and discovery reads it on
  every pass — from any working directory, in any later process. The trust
  review is kept rather than waived: a learned layout's code runs only against
  a recorded content hash, and confirming the Teach is what records the hash of
  the bytes it just wrote, so consent is taken where the operator actually gave
  it and any later edit to `context.py` un-trusts the pack until it is
  confirmed again. If the hash cannot be recorded the Teach FAILS, because a
  draft nothing can select is the same false completion by another route. The
  Charts and Migrate choosers are re-asked the moment a layout is written
  instead of holding the list they were handed at boot, and both name the exact
  directory a run will bind to. A layout that is missing, edited, or untrusted
  refuses the run loudly (exit 2, `bad_pack`) and never falls back to the
  built-in one.

- **The rail read "0 warn, 0 fail" over a chart that abbreviated thirteen
  facts.** `settle_qa` had carried `not_carried` on the QA stage event since
  #271's never-green-with-nothing-said rule, but neither frontend said what it
  meant: the CLI's QA line stopped at pass/warn/fail, and the GUI's rail turned
  the bare key into "not carried 13", which told an operator nothing about
  what 13 counted. Both now read the count in the words `qa_report.json`
  already had — "13 fact(s) carried by the record summary, not the visit
  charts" — printed only when the count is nonzero, silent on every run that
  abbreviates nothing. (#297)
- **A scanned chart is still a chart.** A C-CDA Unstructured Document carries
  its whole clinical content as one embedded or referenced artifact under
  `<nonXMLBody>` — a scanned referral, a faxed discharge summary — instead of
  coded sections. The parser read the header, found no `structuredBody`, and
  returned a patient with nothing on their chart: no error, no skip, a run that
  reported success. In the 6,144-document ledger run that was 1,024 documents,
  every one of them a total loss reported as a clean parse. The adapter now
  carries the artifact rather than refusing it — refusing loses the patient
  entirely, while a record holding demographics plus the scan is what the source
  actually had — recording the `@mediaType` exactly as declared, resolving a
  reference against the document's own directory, and failing closed on the two
  shapes it cannot carry: a referenced file the export does not hold, and an
  artifact over a declared 32 MiB ceiling (never a truncated clinical document).
  Delivery writes it beside the rendered charts in the same hardened
  attachments directory as every other source attachment, under the same
  claimed names, so nothing downstream has to know which artifacts arrived as
  files and which arrived inside their record. `body:nonXMLBody` moves from
  1,024 unsupported to 1,024 parsed; every other ledger row is unchanged. (#313)

- **A migrated C-CDA chart says who wrote it again.** The adapter had no
  extraction for participations at all: across 6,144 generated documents twelve
  construct classes had a parsed column of exactly zero — `author` (17,390
  offered), `performer` (5,122), the custodian, the authenticators, the data
  enterer, the informant, the information recipient, the header participant,
  the service event and the encompassing encounter — and the 2,103-document
  audit saw the same absence from the other end. They were not failing; they
  were never read. The parser now reads the header: each participation becomes
  a `Practitioner` carrying the role the document gave it, so a legal
  authenticator is not filed as an informant and a human author is not
  flattened into the `assignedAuthoringDevice` that generated the summary; the
  organizations they name become `Facility` entries, deduplicated so the
  practice named by the author and again by the custodian is one place;
  `componentOf/encompassingEncounter` becomes the visit the document is about,
  which is the only place a Progress Note states it. `documentationOf/
  serviceEvent` keeps its performer but is deliberately NOT charted as a visit —
  its `effectiveTime` is a care-provision period, and a low bound is not a date
  of service. Ten of the twelve rows now read fully parsed; the authoring device
  and a `relatedEntity` informant are extracted too but cannot be CREDITED,
  because CDA R2 gives neither an `<id>` and the ledger credits a parse only on
  an id root the construct carries — counted in its `unlinkable` column rather
  than assumed. FHIR export types the three kinds apart on the CDA role class
  the record now carries: a clinician is a `Practitioner`, a person in a
  personal relationship with the patient (an emergency contact, a spouse who
  gave the history) is a `RelatedPerson` referencing that patient, and an
  authoring device is a `Device` — a bundle that exported all of them as
  practitioners would show a family member on the care team. All three read
  back into `practitioners` in bundle order, so correct typing costs them
  nothing on the round trip. (#312)

- **A destination pack can describe the filing dialog it actually meets.**
  Attaching the file was the whole vocabulary: a pack could name a file input
  and a submit button and nothing else, so a chart filed through the browser
  route landed uncategorised, undated, in whatever status the form defaulted
  to and under no provider. Seven optional slots now describe the dialog —
  display name, category, status, date, the patient it prefills, provider,
  note — and the page seam gained the two verbs needed to drive and read them
  (`select_option`, `input_value`). Every slot is optional and skipped when
  unset, so an already-discovered `selectors.yaml` keeps meaning exactly what
  it meant. Two of the fields are gates rather than fields: the date the form
  echoes back must be the date it was given, and the patient the dialog
  prefills must still be the one the chart banner confirmed — the last
  wrong-patient check before anything is committed, and the only one that can
  see inside the dialog. (ANV2-005)
- **A run that is happening now says so.** Every view narrated a run in one
  line on screen and none of it reached a screen reader — a click produced
  silence. One always-present polite region carries it, written together with
  the visible line and only when that line actually changed. (#198)
- **The filing calendar is a table you read, not 42 buttons you cannot press.**
  Every cell was a `<button>` with no click handler and a hand cursor, inside a
  `role="grid"` with no rows at all. (#198)
- **Escape closes what Escape opened.** About, the activity drawer and the
  error-kinds flyout were wired separately and each forgot a different part of
  the contract; one implementation now owns `aria-expanded`, Escape,
  click-elsewhere, and moving focus in and back. Teach's mode tabs answer the
  arrow keys. (#198)
- **Four attributes that named nothing**: a `<label for>` pointing at a `<div>`,
  a patients table with no `<thead>` or `scope`, a column-mapping grid that read
  as a table and said nothing about it, and an `aria-activedescendant` emptied
  rather than removed. (#198)
- **An upload that dies says so before it dies.** A `BaseException` from the
  filing engine — how it deliberately models process death — sailed through
  both safety nets, leaving a run that had started, would never finish, and
  told nobody. (#117)
- **A machine that cannot render charts says so once, with the remedy**, instead
  of one bare exception type per chart. (#202)
- **The activity shortcut fired on the wrong keys**: `Ctrl+L` opened the drawer
  instead of the address bar, and a bare `l` opened it on a chooser trigger,
  where the type-ahead had already claimed the character. (#214)
- **The Uploads search truncated twice and mentioned neither cut**, so a visit
  id past the limit looked absent. (#214)
- **The error banner never left.** It cleared on five run entry points and
  nowhere else, so a message about a fixed problem followed the operator around
  the app. It now clears on a view switch and carries a dismiss control. (#214)
- Two charts can never land on one file; a same-day collision widens its suffix
  until the name is free, and two encounters carrying one id are reported
  rather than silently merged. (#186)
- A re-rendered loss ledger keeps every narrative node. (#122)
- Two exports of one record are byte-identical again. (#193)
- The clinical note is verified to have reached the page. (#188)

### Changed

- **Which visits become charts is a run's choice, not the product's opinion.**
  The PF/Tebra adapter kept two shapes of encounter out of the render — a SOAP
  note whose four sections are all empty, and a growth-chart visit for a
  patient who was an adult at the time — and parked them losslessly in
  `extensions`. Both rules are sound for the practice that asked for them and
  neither is universal: an archivist retaining everything wants the empty
  visit, and a paediatric practice whose patients grew up wants the growth
  chart. Each rule is now a per-run option in the shape the section flags
  already have — `anast pipeline run --include growth-charts`, a tick in the
  GUI's new "Visits to skip" matrix, `PipelineCommand.include` between them —
  with every rule on by default, so an existing run is unchanged: the six
  charts, the render index, the render settings, the canonical records and the
  stage events are byte-for-byte what they were. A rule name the source does
  not have is refused before the export is opened, listing the ones it has, and
  `anast info` prints each format's rules beside the word that switches one
  off. The accounting follows the option rather than describing the old run:
  an included growth chart is rendered, graded, and its measurements are on a
  page, where under the rule they were a fact QA could only report as attached
  to a visit the record did not contain. `selection_report.json` is version 2:
  it still names every excluded encounter and the rule that excluded it, and it
  now also names every rule the source has and whether this run applied it —
  without that, an empty `excluded` meant either "the rules found nothing" or
  "no rule was running", which are opposite answers. (#288)
- **A selector under a name the loader does not know is now refused, loudly.**
  It used to be dropped on the floor: the loader read a closed list of slot
  names, so a typo'd or stale key was read by nobody and reported to nobody
  while the pack still announced itself ready — an operator who discovered a
  selector and watched the field stay empty had no way to find out why. This
  is a breaking change for a hand-edited `selectors.yaml` carrying such a key,
  which will now fail to load with the offending name in the message.
  (ANV2-005)
- **A layout or an export format is shown by the name its author typed.** Both
  registries carry a `display` field now; `anast info` leads with the name and
  dims the id beside it. The front end's hard-coded `ccda → "C-CDA"` exception
  is gone, because the registration carries it. (#164)
- Every command's help says what the command actually does, and names a next
  step that exists. (#196, #197, #201, #203)
- Charts already in a folder answer the question being asked. (#189)
- `anast info` says what is installed, not that it is ready — `anast doctor` is
  the command that tries things. (#190)
- The Uploads counters are named for what they count: charts in the record, and
  PDFs in the folder. They are not the same measurement and used to look like
  it. (#214)

### Release

- **One build, two doors: the Windows package job now also emits an MSIX for
  the Microsoft Store.** The installer is unsigned, so every download meets
  SmartScreen; the Store re-signs each package it ingests with Microsoft's own
  certificate, which is a trusted publisher signature this project does not
  have to buy. The new artifact is packed from the SAME Nuitka layout the Inno
  installer packages — no second build, no chance of the two drifting — and
  ships beside the installer and the SBOM on the release. `anast` stays
  invocable by name through an app-execution alias, the MSIX-native answer to
  the installer's optional PATH task. Nothing about the EXE path changed, and
  nothing here signs anything: a package this repo signed would carry a
  certificate nobody trusts. (#292)
- **The shipped SBOMs name a version.** `dynamic = ["version"]` left both
  documents describing a root component with `version: null` — and dropped the
  package itself from the inventory — so the SBOM could not answer the one
  question it exists for. Both workflows now share `tools/sbom.py`, which
  resolves the version and refuses a document that does not carry it. (#142)
- The installer's SBOM is generated **after** the smoke gate, not before: an
  SBOM for an installer that has not been shown to install is a statement about
  nothing. (#142)
- The installed-footprint measurement can no longer fail a release. It is
  informational by design and was not guarded. (#142)

## [0.7.0] — 2026-08-21

The seventh alpha (**0.7.0-alpha**). Two arcs in one release. First the
surgery arc: scope the product to what it does best, make the repo read as a
product, close the FHIR-API delivery loop, and brand the Windows app.

Then the closure of a full-depth external audit — 22 findings, verdict
REJECT for 0.7.0 — and four internal adversarial review rounds on the
fixes themselves. Every finding was reproduced against this tree before it
was fixed and re-proven after; the review rounds found and closed residual
gaps in the fixes, including two that reopened the exact wrong-patient
collision the first fix was written to reject.

### Added

- **FHIR-API delivery route**: `anast upload --fhir URL` drives the same
  engine, ledger, skiplist, and L0-L6 ladder as the browser route. Bearer
  token via environment variable only (`--fhir-token-env`, default
  `ANAST_FHIR_TOKEN`); `--create-patients` (default on) for migration
  targets; ambiguous patient matches always refused. L4/L5/L6 run on this
  route (L3 skips with "no pack provided"); proven end-to-end against a
  live HAPI server in CI.
- **Product branding**: one SVG master (`assets/icon/icon.svg`) drives the
  multi-resolution exe icon, installer wizard imagery, Add/Remove Programs
  entry, and the AppUserModelID taskbar identity (`tools/make_icons.py`
  regenerates every rendition).
- **GUI behaviour lane** (`tests/gui_e2e`, 49 tests): headless Chromium
  drives the bundled pages through a generated pywebview-bridge stub with a
  console-error recorder and a GuiApi drift guard; runs as its own CI job.
- **Installed-binary smoke** (`packaging/smoke_windows.py`): silent install
  -> installed layout -> installed `anast doctor` -> the dashboard rendered
  inside the real WebView2 window (Playwright over CDP) -> silent uninstall
  with leftover check; wired into the Windows package job.
- **One shared identity predicate** (`core/identity.py`): boundary-anchored
  name, date, and value matching behind the L2/L3/L6 delivery verifier, the
  browser pack's row and banner matchers, and the QA integrity check, so the
  wrong-match defense cannot drift into a substring-loose variant in one
  place and not another. Name boundaries treat the whole Unicode
  hyphen/dash family and all three apostrophes as intra-name joiners;
  truncated values (`"Ann Li..."`) reject as unknown identities.
- **Loud refusals that reach the operator**: `OrphanRowsError` (a row on a
  known table whose foreign key names no record), `AmbiguousUnanchoredError`
  (a dangling patient reference alongside several patients), and
  `RedirectRefusedError` (a FHIR endpoint that answers a redirect). Their
  messages carry table/resource-type names and counts only, and reach the
  CLI and GUI verbatim through a `SourceDataError` passthrough.
- **Path budgeting for delivered trees** (`core/textutil.budgeted_name`,
  `deliver/_shared`): every delivered component is capped and every full
  path budgeted, with a 64-bit distinctness tag on any cut name and a
  per-run claimed-name ledger (`DeliveredNameCollision`) so two different
  source ids can never merge into one delivered slot.
- **Spatial rendering goldens**: page-1 word bounding boxes for both packs,
  regenerated by the same `tools/regen_goldens.py` pass as the text goldens
  — a CSS regression that moves a value under the wrong label now fails the
  lane that page-count, geometry, and extracted text all pass.
- **Third-party license texts ship with every artifact**:
  `assets/licenses/APACHE-2.0.txt` and `OFL-1.1.txt` plus a top-level
  `THIRD_PARTY_LICENSES.md` inventory, carried in the wheel
  (PEP 639 `license-files`) and the installer, with a release-workflow step
  that fails the build if the wheel ever loses them.
- **`PayloadTooLarge` preflight** on the FHIR upload route: an item is
  measured before its bytes are read, so an oversized chart is refused with
  an actionable message instead of materialising several times its size in
  memory.

### Changed

- The tebra destination declares its shipped browser pack in
  `destinations/registry.yaml`, so route planning can select browser
  automation (the GUI surfaces the pack chip; not ready until selector
  discovery).
- Verify ladder opens each PDF twice per item instead of five times;
  `fuzzy_contains` is linear; shared `safe_name`/`hash_and_size`/delivery
  helpers replace copy-pasted implementations; archive/bundle CLI commands
  register from one factory; duplicated page JS consolidated in `shell.js`.
- PyMuPDF is imported as `pymupdf` (the `fitz` alias is deprecated
  upstream); packaging ships it as bytecode (MSVC heap exhaustion) and
  force-includes the modules the pack contexts import (derived from their
  own import statements).
- Vendor EHI spec binaries (~30MB, non-redistributable) removed from the
  repository and its history; `docs/vendor_refs/` cites the public Oracle
  pages instead.
- Authorship and AI-assistance attribution consolidated in `DESIGN.md`;
  per-file citation banners removed (`tools/cs50_citations.py` re-applies
  them on an academic-submission branch).
- The upload engine reads the wrong-patient banner **before** the duplicate
  scan: a chart's existing-documents list is never trusted until the open
  chart is confirmed to be the right patient.
- The engine threads its already-resolved `DestinationPatient` into
  `verify_pre`, so verification never re-resolves through a
  create-capable path (a second resolve could POST a duplicate patient).
- The PHI scanner is **default-deny** for content it cannot read: binaries
  and base64-armored payloads inside text files pass only when the whole
  file's sha256 carries a provenance entry in `tools/phi_allowlist.txt`.
- `configure_logging` brings **every** root handler into the redaction
  chain, including handlers a host installed before importing Anastomosis.
- Windows output-directory hardening is reset → grant → strip → **verify**:
  the DACL is read back and every entry checked against the granted set,
  and an unparseable or unexpected descriptor fails closed.
- CI's mypy lane installs the documented `.[dev,gui]` environment through
  `packaging/constraints.txt`; the `gui` extra is bounded
  `pywebview>=6.2,<7.0` (6.2.1 breaks Nuitka-frozen Windows builds,
  upstream #1817) and the package build pins `==6.2`.
- README states what the runtime does: `migrate` writes a structured C-CDA
  payload, the FHIR seam is the CLI upload route today, and per-route L-level
  coverage is named honestly.

### Fixed

- `anast --help` no longer advertises commands that do not exist.
- Segment toggles were mouse-dead under pointer capture; pages without a
  log strip now surface errors in the banner; an inline style refused by
  the pages' CSP is set via CSSOM (`gui/web/shell.js`).
- The exe version-info and installer copyright name the project's actual
  license (AGPL-3.0-or-later, not MIT).
- Dead surface removed (verified unreferenced): `bmi_imperial`,
  `RenderIndex.unattributed`, ~300 lines of orphaned GUI CSS, unused pack
  tokens, entry-point pack discovery, the parallel upload runner, and the
  never-populated HealthConcern/ImplantableDevice/LabOrder model family
  (their PF chart sections render statically; golden output byte-identical).
- **Wrong-patient collisions at every identity gate**: an expected
  `"Ann Li"` matched inside `"Joann Liang"`, `"Mary-Ann Li-Wong"` (any
  hyphen codepoint), and `"O'Brien"`-style compounds, while a colliding
  unpadded date of birth matched inside a longer one — each accepted the
  wrong chart at the browser row, the banner readback, and the L2 fast path.
  A reordered compound surname passed the row matcher; the resolver clicked
  row 0 regardless of which row it had matched.
- **Silent data loss across three adapters and the exporter**: rows whose
  foreign key named no record, surplus columns on demographics side rows,
  unread name sub-keys and US Core race codes, section narratives the
  structural parsers could not consume (including duplicate section codes
  that overwrote each other), and record-level extensions that never
  reached the C-CDA loss narrative. The declared-loss oracle was
  value-in-haystack and masked real losses through cross-field collisions;
  it is now path-aware.
- **The Windows GUI now starts and renders in the shipped WebView2 window**:
  pywebview 6.2.1 failed to import from the frozen build (pinned to 6.2),
  the smoke discarded the only diagnostic the dying app produced (both
  streams are captured and printed on failure), and its CDP attach could
  never work — pywebview sets WebView2's browser arguments programmatically,
  so the environment variable the smoke relied on was ignored. The
  debugging port is now an opt-in, diagnostics-only setting.
- `safe_name` returned unbounded components, so a long source id produced a
  path the filesystem refused; the chart-PDF copy was unbudgeted and its
  failure was logged and skipped past — a chart silently missing from a
  delivered tree.
- The uninstall leftover check asked only for `*.exe`, missing DLLs, fonts,
  bundled Chromium data, and logs.

### Security

- The FHIR client **refuses every redirect**. `urllib`'s default opener
  follows redirects and re-attaches request headers, so a server-chosen
  target could receive the endpoint's bearer authorization; the client now
  raises rather than follow, and the operator is told to configure the
  final URL.
- Both release workflows pass ref and tag names to shell steps as quoted
  environment values. Git permits quotes, semicolons, and backticks in a
  ref name and the `v*` filter accepts them, so interpolating a tag into a
  `run:` block was shell injection inside jobs holding `id-token: write`.
- The PyPI release refuses a tag that does not name the version the source
  builds, before anything is built — the invariant the Windows release path
  already enforced.

## [0.6.0] — 2026-07-12

The sixth alpha (**0.6.0-alpha**). Closes the external v0.5.0 review's four
P1 truth defects — every one a case of a claim exceeding the runtime, the
exact defect class this product exists to prevent — plus its confirmed P2s.

### Fixed

- **`migrate` no longer reports route availability as delivery**
  (`core/migration_status.py`) — a chosen route classified the run as
  `DELIVERED` even though `run_migration` executes no route (and Tebra's
  `ccda_import` is a manual in-product import). New three-outcome contract:
  `PREPARED` (route chosen; charts, C-CDA payload, upload manifest, and route
  plan written — delivery NOT executed) is what the flow earns today;
  `DELIVERED` is reserved for a future destination executor with a durable
  receipt, and a test pins that classification never returns it until then.
  Both frontends print an actionable prepared-notice; exit codes unchanged.
- **`--render ccda-standard` no longer bypasses requested QA**
  (`core/migrate.py`) — the mode ignored `--qa` entirely (no events, no
  report, exit 0). The document-generic checks (data-integrity leak
  detection, layout/pagination) now run per rendered patient document,
  `qa_report.json` is written, FAIL exits nonzero, and the encounter/pack-
  scoped checks are recorded as skipped with a reason.
- **GUI no longer displays failed uploads as success**
  (`core/upload_command.py`) — the CLI failed non-clean terminal counts
  while the GUI checked only the abort reason, so a wrong-chart
  `PRE_VERIFY_FAILED` run read "upload complete". The verdict (`is_clean`,
  `exit_code`, PHI-safe non-clean summary) now lives on
  `UploadCommandResult` in the shared core; both frontends consume it.
- **No operator output path in logs** (`deliver/ccda_export/deliverer.py`) —
  the export-complete log carried the full output directory; operators name
  directories after patients. Count-only now, with caplog regressions.
- **Cross-page GUI events** (`gui/events.py`) — every event now carries a
  `flow` tag and each page filters to its own flow, so navigating mid-run
  can no longer make the wizard announce another flow's completion; the
  window's close path now refuses to silently interrupt a running job.
- **Pack-trust hash gate is race-free** (`reconstruct/packtrust.py`) — the
  external-pack hash was computed from one read and the code executed from a
  second, so a local writer could swap content between check and use. The
  executable surface (`context.py`) is now execution-pinned to the same
  single-read snapshot the hash covered, and `pack.yaml` is parsed from
  pinned bytes; `template.html` contributes to the hash and is
  presence-checked but still renders from disk (a bounded, non-importing
  Jinja surface — render-from-snapshot is on the backlog), and auxiliary
  assets are documented as outside the hash. Trust-store writes re-read,
  merge, and atomically replace under the repo file lock.
- **Upload resources register with the ExitStack the instant they are
  owned** (`core/upload_command.py`) — a ledger/verifier construction
  failure used to leak the attached Playwright driver.
- **Unsourced README statistics removed** — the "73% of organizations" and
  "$5,000–$150,000" figures traced to phantom citations with no primary
  source; the problem statement now makes the qualitative case on cited,
  peer-reviewed evidence (as `paper/paper.md` always did).

### Changed

- **PF mapper builds its encounter link-table indexes once per run** (the
  two per-encounter whole-table rescans the earlier hoists never covered) —
  ~9× faster encounter mapping on a 300-encounter probe, output
  byte-identical by goldens.
- **QA extracts each PDF once per run** instead of up to four times (one
  snapshot per document cached on the context, with a fallback so
  third-party QA packs keep working) — report byte-identical.
- **FHIR Bulk ingest streams NDJSON into the grouping index** instead of
  double-buffering per file; the memory expectation (resident memory scales
  with export size; spooling is roadmap) is documented instead of implied
  away.
- C-CDA conformance claims aligned to the code: the export is validated by
  round-trip with this repo's own parser; CDA XSD structural validation is
  blocked on two now-documented exporter gaps (mandatory `author`/
  `custodian` participations, OID id roots) and recorded, with full
  Schematron conformance, on the backlog.
- `python-dateutil` removed (declared, never imported); predecessor
  line-reference comments (`gpdfs:`) and stale worklog tags rewritten as
  present-tense invariants, with the archaeology guard extended to ban the
  tokens.

## [0.5.0] — 2026-07-03

The fifth alpha (**0.5.0-alpha**). Two fixes from the external alpha-4 review
plus the piece that makes the Windows app real for users: a release path that
actually ships the installer.

### Fixed

- **Raw source identifiers no longer enter logs** (`core/logutil.py`
  `safe_log_id()`) — the log contract said "opaque ids", but the ids being
  logged were the source systems' own GUIDs (`PatientPracticeGuid`,
  `PERSON_ID`, encounter/event ids, and the upload `item_key` that embeds an
  encounter GUID) — linkable, not opaque, on the machine where the export
  lives. Every logging site that interpolated one now routes it through
  `safe_log_id()`: an HMAC-SHA256 surrogate keyed per process, so log lines
  about the same record still correlate within a run but are unlinkable
  across runs and cannot be confirmed against the export. Display surfaces
  (CLI failure lines, upload console, resumability ledger) deliberately keep
  real ids — they are operator working surfaces inside hardened directories,
  not logs. SECURITY.md states the contract precisely; the caplog tests
  assert the surrogate form and the absence of the raw id.
- **The PHI scanner works without a git checkout** (`tools/phi_scan.py`) —
  it enumerated files via `git ls-files`, so users running the test suite
  from a source ZIP or sdist got a scanner crash instead of a scan. When git
  enumeration is unavailable it now falls back to a deterministic recursive
  walk with an explicit prune set (VCS/cache/venv/build directories); under
  git, behavior is unchanged. A scanner that silently skipped non-git users
  would have been a hole, not a fallback.

### Added

- **Publish a release from the Actions tab** — the Windows-package and PyPI
  workflows gain a guarded `workflow_dispatch` publish mode (main-only,
  version-asserted) in which the release action creates the `v<version>` tag
  itself. The 0.4.0 installer was built and CI-validated but never reached
  the Releases page because publishing required a terminal tag push; that
  hard dependency is gone.
- **Installer polish** (`packaging/anastomosis.iss`) — optional desktop-icon
  task, `UninstallDisplayIcon`, and the AGPL license page in the wizard. The
  launch-Anastomosis-on-finish checkbox already existed. Known GA gaps,
  documented: code signing (needs a purchased certificate; SmartScreen
  guidance is in the README) and a bespoke application icon.

## [0.4.0] — 2026-07-03

The fourth alpha (**0.4.0-alpha**). Closes the external release review with
hardening rather than suppression, and turns the repository into a complete,
self-explaining product: real Windows PHI-at-rest protection, log redaction
that is actually installed, executable (not decorative) CodeQL policy, a
design/authorship record (`DESIGN.md`), and the remaining size hotspots split
behind stable facades. No new feature surface — this release's job is
trustworthiness.

### Added

- **Windows PHI-at-rest hardening** (`core/output.py`) — `secure_output_dir`
  now hardens every output directory on Windows NTFS: ACL inheritance
  stripped, access restricted to the current user, SYSTEM, and Administrators
  (the posture CPython adopted for `os.mkdir(mode=0o700)` in the
  CVE-2024-4030 fix, and Win32-OpenSSH uses for key material), via `icacls`
  with literal, localization-safe SIDs and fail-safe ordering (grant before
  inheritance strip — no failure mode can lock the operator out). ACL-less
  filesystems (FAT32/exFAT) degrade to a loud warning; the PHI-warning README
  lands regardless. POSIX behavior (`0o700`) is unchanged. A real ACL
  assertion runs in the Windows CI lane.
- **CodeQL, for real** (`.github/workflows/codeql.yml`) — an advanced-setup
  workflow (push / PR / weekly) with the `security-extended` suite, whose
  built-in `AlertSuppression.ql` query honors inline `# codeql[rule-id]`
  comments with no extra pack, placed at exactly the audited PHI-by-design
  write sites — no rule is excluded repo-wide. Each suppression sits beside
  a `PHI-BY-DESIGN` rationale
  comment, and a policy test pins that every suppression carries one. (The
  repository's code-scanning *default setup* must be disabled once in
  Settings for the workflow's uploads to be accepted — GitHub rejects
  advanced SARIF while default setup is on.)
- **`DESIGN.md`** — the design record: architecture, data model, the
  genuinely debated decisions, hardest problems, verification strategy, and
  the authorship record.
- **Single-sourced Playwright pin** (`packaging/constraints.txt`) — the CI
  e2e lane and the Windows packaging build both resolve Playwright through
  one constraints file (`pip install -c`); the Windows browser-cache key
  derives from the file's hash; a drift test pins the whole arrangement
  (library floor stays open for users — builds pin).
- A review-archaeology CI guard: a test that bans review-history tokens from
  src/, tests/, tools/, and workflows — comments state invariants; history
  lives in this changelog.

### Fixed

- **Log redaction is now actually installed.** `configure_logging()` — the
  only code that installs the `RedactionFilter` — existed but was never
  called, so production logging ran unredacted through Python's last-resort
  stderr handler. The redacting handler is now installed idempotently at
  both entry points (the CLI root callback and the GUI main), the filter
  learned the `MM-DD-YYYY` filename date shape, and a pipeline-level
  regression test asserts no fixture patient name can appear in any log
  record.
- **The one patient-derived log message.** The archive deliverer's
  missing-PDF warning logged a rendered filename that embeds patient name +
  date of service; it now logs the opaque patient id. Remaining messages
  that interpolated paths under an output directory were aligned with the
  repo convention (never a path under out_dir) and their tests updated.
- One vulnerability-reporting SLA: the root `SECURITY.md` became the single
  reporting policy (72-hour acknowledgement, coordinated disclosure); a stale
  unchecked CDP-attach backlog item (shipped in 0.2.0) was corrected in
  `docs/PLAN.md`.

### Changed

- **The size hotspots are split behind stable facades** (the "post-beta"
  refactor, pulled forward): `cli.py` (1,650 lines) now delegates its command
  groups to focused modules while remaining the import facade — every public
  symbol, monkeypatch seam (`cli._make_destination`, `cli.console`), entry
  point, and help string is preserved and pinned by the existing boundary
  and encoding tests; `UploadConsole.upload_start` (167 lines) is decomposed
  into its pre-flight and worker stages. The PF mapper was evaluated and
  deliberately left whole (already function-decomposed; goldens pin its
  output byte-identical).
- Review-history comments across src/, tests/, tools/, and CI were rewritten
  as invariant comments — each now states the property it pins, not the
  review that requested it.

## [0.3.0] — 2026-06-24

The third alpha. Packages the toolkit as a downloadable Windows application — a
normal installer that bundles its own Python runtime and Chromium (with no
separate `pip`/`playwright` step) and installs the Edge WebView2 runtime when it
is absent — and clears the last CLI/GUI disparities so the backend CLI and the
desktop GUI drive identical shared command cores rather than parallel
implementations. The packaging build, the installer, and a silent
install-and-self-check are produced and validated on Windows CI. PR numbers in
parentheses.

### Added

- **Downloadable Windows application** (`packaging/`,
  `.github/workflows/windows-package.yml`) — two Nuitka `--mode=standalone`
  executables (the windowed GUI app and the `anast` console CLI), each bundling
  the Python runtime, Chromium, and every data asset, packaged by Inno Setup
  into a single installer with a Start-menu shortcut, an uninstaller, an optional
  "add `anast` to PATH" task, and a silent Edge WebView2 install when the runtime
  is absent. No separate `pip install` or `playwright install` step. Built and
  self-checked on Windows CI — installed silently and re-checked end to end — and
  attached to the GitHub release on a version tag.
- **`anast doctor`** (`core/selfcheck.py`) — a bundled-asset self-check that
  resolves and reads every shipped asset (the destination registry and tebra
  pack, both built-in template packs, the GUI web pages and fonts, the HL7
  `CDA.xsl` and its siblings, the learned-source synonym and schema files, the
  archive assets) and, in a frozen build, confirms the bundled Chromium is
  present. CI runs it against the FROZEN executable before packaging and again
  against the INSTALLED executable after, so a mis-bundled asset fails the build
  instead of reaching an operator. (#63)
- A standalone GUI entry point (`anastomosis.gui.__main__` plus a `gui-scripts`
  console entry) that the installed Start-menu shortcut targets. (#63)
- **L0–L6 verification ladder around uploads** (`anast upload` + a GUI "Verify
  uploads" toggle) — the implemented `LayeredVerifier` (`deliver/verify/`) is now
  reachable from both frontends through the shared upload command: L0 file
  integrity, L1 page/size, L2 document identity (fuzzy name ≥0.88 + DOB
  hard-fail), and, after upload, L5 metadata and L6 round-trip read-back. It runs
  by default (see Fixed); the engine's live wrong-patient banner abort runs on
  every upload regardless.

### Changed

- **One shared command core per flow, consumed by both the CLI and the GUI**, so
  the two frontends cannot diverge: migration-status classification
  (`core/migration_status.py`, #57), the upload command (`core/upload_command.py`,
  #58), and the source-init command (`core/source_init_command.py`, #59). The GUI
  learn-a-source wizard now runs asynchronously, like the pipeline, migrate, and
  pack flows. (#59, #60)
- The Practice Fusion pack's `build_context` is decomposed into focused,
  output-preserving helpers, and the flowsheet's vital-by-encounter scan is built
  once per record instead of once per encounter. Rendered output is byte-identical.
  (#61, #62)

### Fixed

- **The CLI no longer crashes on a non-UTF-8 (e.g. CP-1252) Windows console.** A
  new `core/presentation.py` resolves Unicode versus ASCII glyphs from the output
  stream's encoding; the transit map and every arrow-printing line use it, and
  the ASCII markers are bracket-free so Rich does not strip them. UTF-8 output is
  unchanged. (#56)
- The GUI surfaces a no-automated-route migration as an error and manual-import
  path instead of a silent success, matching the CLI (which already writes the
  C-CDA payload and exits non-zero). (#57)
- Upload `max_attempts` is unified to 3 across the CLI and GUI, the GUI gains a
  `--skiplist`, and the GUI now acquires the busy-guard and output lock BEFORE
  reading the upload manifest, closing a lock-then-read race. (#58)
- All five GUI async methods (`run_pipeline_async`, `run_migration_async`,
  `pack_init_async`, `source_init_async`, `upload_start`) now guard the
  worker-thread spawn: if `Thread.start()` fails, they release the busy flag and
  return a clean error dict instead of propagating to the bridge and wedging the
  GUI in "Busy".
- **No silent table loss in the Practice Fusion adapter.** The loader now reads
  EVERY `*.tsv` in an export (not just the 30 it maps); the mapper preserves each
  unmapped table's rows verbatim in the owning patient's `extensions`, and refuses
  the run (`UnsupportedTablesError`) when a table's rows cannot be attributed to a
  known patient — failing closed rather than discarding clinical data (e.g. an
  unmapped `patient-procedures` table).
- **Upload verification is ON by default and fails closed.** `anast upload` (and
  the GUI) now run the L0-L6 wrong-chart/wrong-patient ladder unless the operator
  explicitly passes `--no-verify`; if the render extra the ladder needs is absent,
  the run is refused rather than filing unverified. Filing into the wrong chart is
  worse than not filing.
- **Upload lock fences the migrate layout too.** `run_upload_command` now locks
  both the output dir AND the resolved manifest root (a `migrate` writes/locks the
  manifest under `<out>/charts`, a different lock dir), closing the lock-then-read
  race for that layout.
- **Windows installer "add to PATH" correctness:** the optional task writes the
  MACHINE `Path` (the install is per-machine/elevated, so the per-user HKCU hive
  would have been the elevating admin's, not the user's); it records an
  installer-owned marker and strips the entry on uninstall ONLY when that marker
  is present (delimiter-anchored), so a pre-existing or manually-added entry is
  never removed; and `ChangesEnvironment=yes` broadcasts the change so a new shell
  sees `anast` without a logout.
- **Windows package integrity:** CI now self-checks the FROZEN GUI bundle
  (`Anastomosis.exe --self-check`, the Start-menu target), not only the CLI bundle;
  the `anast doctor` tebra-pack check targets the BUNDLED pack specifically (a user
  pack can no longer mask a missing built-in); the WebView2 bootstrapper download
  is Authenticode-verified (signer = Microsoft); the release action is pinned to a
  commit SHA; and a release tag is asserted to equal `v<version>`.
- **GUI dashboard runs can now produce the upload manifest** the upload console
  consumes (a "write upload manifest" toggle threading `write_manifest` through
  `run_pipeline_async`) — GUI parity for `pipeline run --upload-manifest`. The
  upload console also gained the `--pack-dir` parity it was missing (it had
  hard-coded `null`).
- **The GUI bridge exposes only safe methods.** A `GuiApi` facade is bound as
  pywebview's `js_api` instead of the raw controller, so the synchronous heavy
  methods (`run_pipeline`/`run_migration`/`pack_init`/`source_init`) and `doctor`
  (which can start Playwright) are no longer callable from JS and cannot freeze
  the bridge; the front end uses the `*_async` variants.
- **Per-run GUI result summaries** are keyed by an opaque `summary_id` carried on
  the `done` event, so a rapid second run can no longer overwrite the per-patient
  detail the first run's UI is about to read.
- **Browser-upload teardown owns its Playwright resources.** When a run ends,
  `run_upload_command` releases the Playwright driver + CDP connection it owns
  (`browser.close()` then `playwright.stop()`, which per Playwright only
  disconnect a `connect_over_cdp` browser) — never the operator's EHR browser,
  and distinct from the manager's per-recycle session `close()`.
- **Cross-platform CI hygiene:** `core/locking.py` type-checks cleanly under
  `mypy --platform win32` (the fcntl/msvcrt branches are `sys.platform`-guarded);
  the packgen body-font e2e test accepts Windows's serif rendering
  (`TimesNewRomanPSMT`), not only a literal "Serif".

## [0.2.0] — 2026-06-15

The second alpha. Generalizes ingest and output so a migration is "from any EHR
to any EHR": a FHIR R4 source adapter, a standard HL7 C-CDA render view, an
`anast migrate` from→to command, and — when the toolkit meets a structured export
it has never seen — the ability to learn that format from a single example. Adds
the browser-upload delivery engine (a CLI and a GUI console that drives it), a
cited destination-capability registry with a shortest-path router, the
pack-from-samples layout learner, and the desktop GUI, all on the v0.1.0
foundational pipeline. PR numbers in parentheses.

### Added

- **Learn a new source format from one example** (`anast source init`,
  `sources/learned/`, `core/sourcelearn.py`, `core/model_paths.py`) — when a flat
  CSV/TSV/JSON/NDJSON export is not recognized, teach it once: a local, PHI-safe
  analysis profiles each column (counts, inferred types, digit/letter-masked
  shapes — never a raw value) and a deterministic matcher (column-name similarity
  via `rapidfuzz` + a shipped synonym table + value-type affinity) proposes a
  mapping to the canonical model, which the operator confirms. The mapping is
  saved as declarative DATA — a validated `MappingSpec` with a closed transform
  verb table, no executable code — auto-detected thereafter by a column
  fingerprint and shareable by copying its directory. Unmapped columns are
  preserved losslessly in `extensions`, and a round-trip proves no column is
  dropped before the mapping is saved. A matching GUI wizard ships too. (#49, #50)
- **`anast migrate --from <source> --to <destination>`** (`core/migrate.py`) —
  the from→to composition: ingest any source, plan the delivery route, and emit
  BOTH the human-readable charts AND the structured C-CDA payload the target
  imports. Re-runnable migration profiles persist in `~/.anastomosis`. PF→Tebra
  becomes the special case `--from pf-tebra --to tebra`. (#46)
- **FHIR R4 / US Core source adapter** (`sources/fhir_r4/`) — ingests a FHIR R4
  Bundle or a Bulk-Data `$export` NDJSON directory into canonical records,
  deterministically and source-traced; unmapped fields → `extensions`. (#44)
- **Standard C-CDA render mode** (`reconstruct/ccda_standard/`,
  `anast migrate --render ccda-standard`) — renders the structured C-CDA payload
  through HL7's own vendored `CDA.xsl` stylesheet to a neutral, vendor-agnostic
  per-patient PDF with no network egress, so a migration is never dressed in
  another vendor's house style. (#45)
- **`anast upload` + a GUI upload console that drives it** (`cli.py`,
  `gui/web/console.{html,js}`) — file reconstructed charts into a destination EHR
  through its web UI over a loopback-only DevTools attach, resumable across a hard
  kill; the GUI console starts/stops/monitors a run against the same ledger and
  never closes the operator's browser. (#47, #54)
- **Browser-delivery safety spine** (`deliver/browser/`) — the 15-state upload
  state machine (`UploadState` + legal-transition graph) over a WAL-mode SQLite
  ledger that survives a hard kill mid-upload, with a `FakeDestination` test
  double and a kill-and-resume test. (#13)
- **Upload engine** (`deliver/browser/engine.py`) — drives one item through the
  state machine: patient resolve, duplicate scan, pre/post verification, upload,
  bounded retry, and a skiplist; loud, PHI-safe permanent vs. transient failure
  classes. (#14)
- **Parallel workers, session manager, CDP attach, and run reports**
  (`deliver/browser/{parallel,manager,cdp,reports}.py`) — bounded concurrency,
  a session/manifest manager, loopback-only Chrome DevTools Protocol attach
  (never stores credentials), and PHI-safe run reports. (#15)
- **L0–L6 verification ladder** (`deliver/verify/`) — the wrong-patient
  defense: L0 file integrity, L1 page/size, L2 identity fuzzy match (≥0.88) with
  a date-of-birth hard-fail, L3 pack-driven header fields, L4 live patient-banner
  readback, L5 destination metadata, L6 byte/identity round-trip; stacked behind
  the engine's verifier seam. (#16)
- **Capability registry + shortest-path router** (`destinations/registry.py`,
  `destinations/registry.yaml`, `deliver/router.py`) — destinations declare
  capabilities as cited data; the router picks vendor API → C-CDA import →
  browser automation, and never routes an `unverified` capability. (#17)
- **Browser destination packs + discovery wizard** (`destinations/browserpack.py`,
  `destinations/wizard.py`, `destinations/tebra/`, `anast destination init`) —
  the Tebra pack ships with DISCOVER-placeholder selectors discovered by the
  operator against their own session; no vendor DOM is ever invented. (#18)
- **FHIR R4 API pusher** (`deliver/fhir_api/`) — a stdlib-`urllib` FHIR R4 REST
  client that files charts as `DocumentReference` resources (https, or http only
  for loopback), validated against a HAPI/Medplum-style integration service. (#19)
- **C-CDA export deliverer** (`deliver/ccda_export/`) — `PatientRecord` →
  C-CDA R2.1 / CCD XML for destinations that import C-CDA, with this repo's own
  C-CDA parser as the read-back contract. (#20)
- **Golden rendering tests + Synthea e2e lane** — text-and-geometry
  golden tests pinning Chromium output, plus an end-to-end pipeline lane over a
  vendored synthetic Synthea C-CDA sample. (#21)
- **Layout-learner harvest + inference** (`packgen/extract.py`,
  `packgen/infer.py`) — PyMuPDF-only, fully offline span/drawing harvest and
  deterministic, explainable inference (type scale, column grids, design tokens,
  section taxonomy, static-text intersection). (#22)
- **Layout-learner draft-pack emitter + wizard** (`packgen/emit.py`,
  `anast pack init --from-samples`) — writes a loadable draft template pack
  (mirroring `generic_soap`) with a same-patient confirmation gate and a DRAFT
  provenance note. (#23)
- **GUI shell + headless controller + pipeline dashboard** (`gui/`) — a
  pywebview shell over a fully testable, never-raising controller and thin
  vanilla-JS pages; the liquid-glass dashboard drives the *same* pipeline core
  as the CLI with live ingest/reconstruct/QA counters. (#24)
- **Migration wizard, section-selection matrix, upload console, and
  pack-init UI** (`gui/web/`, `gui/controller.py`) — the transit map as the
  wizard centerpiece, section-flag toggles on the run form, an upload
  console over the 15-state ledger (exception-TYPE histograms only, opaque item
  keys in the Cmd+K palette), a vendor-change freshness toast, and the
  pack-init page with the same-patient confirmation gate. (#25)
- **Frontend-free pipeline core** (`pipeline.py`) — extracted from the CLI so
  the CLI and GUI drive identical code, emitting PHI-safe `StageEvent`s. (#24)
- **Oracle Health / Cerner Millennium EHI adapter** (`sources/oracle_ehi/`) —
  ingests the single-patient V500 export (`v500/{schema,activity,reference}`
  MySQL dumps) via a dependency-free, tolerant `INSERT`-statement reader that
  raises loudly on malformed SQL. Maps the PERSON/ENCOUNTER/CLINICAL_EVENT
  spine plus the §4 notes pathway (CE_BLOB local text, CE_BLOB_RESULT remote
  document *references* — never fetched), resolves `*_CD` through CODE_VALUE,
  filters to current row versions, and routes every unconsumed column to
  `oracle_ehi:` extensions. CE_BLOB compression (brief §8 could-not-determine)
  is a loud `NotImplementedError`, not a guess; PHI-safe logging throughout.
- **Practice Fusion SOAP-note template pack** (`packs/practice_fusion_soap/`) —
  the 35-section forensic PF chart replica, re-typed from the predecessor's
  gold standard: 3-column PATIENT/FACILITY/ENCOUNTER header, the unified 6-column
  demographics table, active/inactive insurance + payment, vitals + vitals
  flowsheet, diagnoses, drug/food/environmental allergies, current/historical
  medications with the ESCRIPT/SCRIPT prescription lines, immunizations, the 17
  social-history sub-categories, PMH, family/advance-directive/devices/health-
  concerns/goals, SOAP, orders, screenings, observations, quality of care, care
  plan, and the conditional addenda table. Honors the documented engine lessons
  exactly (forensic `#f1f1f1` band, `print-color-adjust: exact`, the
  border-collapse "3 lines not 4" rule, the `orphans/widows: 2` + page-break
  rules, Letter geometry with the `.6/.38/.44/.39in` margins) with all real
  clinic identity synthesized (neutral placeholder logo + footer URL, providers
  from synthetic fixtures). Ships a PF golden lane and a packgen fixed-point
  re-discovery e2e (the learner recovers the pack's section taxonomy + band fill
  from its own renders). `RULES.md` records the forensics; `tools/regen_goldens.py`
  now regenerates every pack's golden. (#4)

### Changed

- **Shared pack-init command core** (`core/packinit.py`) — `anast pack init` and
  the GUI now run one analyze→confirm→emit flow; the GUI variant runs off the
  bridge thread so the window stays responsive. (#51)
- **GUI migrate-wizard parity** — the wizard exposes the same pack-dir / trust /
  force / section / QA levers the CLI's `migrate` does, threaded through to the
  same migration core. (#52)
- **Per-record render index built once per record**, not once per encounter — a
  pure-performance change; rendered output stays byte-identical (the e2e goldens
  prove it). (#53)

### Fixed

- **Clean errors on bad / empty / no-route input** — a malformed or empty export
  now fails with a clean exit 2 (PHI-safe, exception-TYPE name only) instead of a
  raw traceback or a silent zero-document "success"; an `anast migrate` to a
  destination with no viable automated route still writes the importable C-CDA
  but exits 1 loudly; and a run locks every output directory, not just the charts
  dir. (#48)
- **Guarantor mapping read invented columns** — the `pf_tebra` adapter's
  `patient-guarantor.tsv` mapping now reads the predecessor-verified column
  set (`BillingPatientRelationshipOption`, `BillingPaymentType`,
  `DateOfBirth`, `BillingGenderOption`, `SSNumber`, bare `City`/`State`/`Zip`,
  `PrimaryPhoneNumber`/`SecondaryPhoneNumber`), so payment preference, DOB,
  sex and SSN populate on a real export instead of silently coming up empty;
  unmapped guarantor columns stash losslessly into the new
  `Guarantor.extensions`. The PF pack's payment cells render the
  predecessor's exact empty states (`-` everywhere, `Primary Insurance`
  preference default) — a present-but-sparse guarantor previously printed
  literal `None` into the PDF. (#4)
- **Windows tracking race** — set the SQLite `busy_timeout` before switching to
  WAL `journal_mode`, fixing a Windows CI race in the upload ledger. (#15)
- **Tracking busy-timeout on slow CI** — raised the ledger busy timeout to 30s
  because `synchronous=FULL` commits could starve the prior 5s window on CI. (#20)

### Security

- **CDP attach is loopback-only** — the DevTools Protocol attach refuses
  non-loopback hosts, warns on shared machines, and never stores credentials. (#15)
- **FHIR client URL guard** — the FHIR base URL must be https (or http only for
  a loopback host); errors carry status codes and resource TYPE names, never
  patient-derived values. (#19)
- **No-hallucination capability registry** — any non-`none` destination
  capability must carry a `source_url` and `verified` date or registry
  validation fails loudly; `unverified` capabilities never route. (#17)
- **No invented vendor DOM** — the Tebra browser pack ships only DISCOVER
  placeholders; real selectors are operator-discovered per tenant via the
  wizard and stored in a user overlay file. (#18)
- **PHI-safe layout learner** — sample PDFs may be named after patients and
  contain per-patient data, so `packgen` stores opaque sample indices, suppresses
  single-sample static/per-patient inference, and restates the same-patient
  caveat in the emitted `DRAFT.md`. (#22, #23)
- **Pack logo cannot reach the network or the filesystem at large** — the
  PF pack's `tokens.logo_data_uri` override accepts only inline `data:` URIs
  (an http/https/file URL would make Chromium fetch it while rendering PHI),
  and `tokens.logo_asset` refuses paths that resolve outside the pack root. (#4)

## [0.1.0] — 2026-06-11

First release: the complete foundational pipeline — one command from a raw EHI
export to verified, human-readable chart documents and a searchable offline
archive. Everything below shipped across PRs
[#1](https://github.com/AzalDaniel/Anastomosis/pull/1),
[#8](https://github.com/AzalDaniel/Anastomosis/pull/8),
[#9](https://github.com/AzalDaniel/Anastomosis/pull/9), and
[#10](https://github.com/AzalDaniel/Anastomosis/pull/10).

### Added

- **Canonical clinical model** (`core/model/`) — lossless, FHIR R4-aligned
  pydantic v2 core: Patient, Practitioner, Facility, Encounter (SOAP note
  sections + addenda), Observation (vitals + social history), Condition,
  AllergyIntolerance, MedicationStatement/Request (e-script transactions),
  Coverage, FamilyMemberHistory, Immunization, AdvanceDirective,
  DocumentArtifact, PatientRecord. Every model carries an `extensions` dict so
  no source field is ever silently dropped. (#1)
- **Core utilities** (`core/`) — sentinel-safe parsing (`\N`, `-1`,
  `1/1/0001` return `None`, never fake values), 7-format date parsing,
  zoneinfo-based local-time conversion, phone/age/HTML sanitizers, LOINC
  vitals map with unit-aware BMI auto-calculation. (#1)
- **Practice Fusion / Tebra source adapter** (`sources/pf_tebra/`) — joins the
  29-table PF EHI v9 export graph into patient records; lossless `extensions`
  enforced per table; e-script status priority resolution. Built and tested
  against a fully synthetic fixture set. (#1)
- **C-CDA / CCD source adapter** (`sources/ccda/`) — ingests C-CDA R2.1
  continuity-of-care documents: problems, medications, allergies,
  immunizations, vitals, results, encounters, notes, social history;
  unmapped sections preserved under namespaced extension keys. (#9)
- **FHIR R4 export/ingest** (`core/fhir/`) — standard resources with exact
  round-trip: export a PatientRecord to a FHIR R4 Bundle and re-ingest it
  back to an identical record, proven by tests. (#8)
- **Reconstruction engine + template packs** (`reconstruct/`, `packs/`) —
  Jinja2 + Chromium rendering with renderer recycling, crash relaunch,
  deterministic filename-collision handling, and idempotent skip; defensive
  pack registry (a broken pack is diagnosed and disabled without taking the
  system down); built-in `generic_soap` pack with user-togglable section
  flags. (#1)
- **QA engine** (`qa/`) — every rendered document is verified:
  data-integrity (placeholder/unresolved-template leak detection),
  layout/pagination, LOINC vitals presence, and date-staleness checks with
  boundary-anchored matching; mutation-corpus self-tests; `--qa` pipeline
  stage exits nonzero on FAIL. (#1)
- **Offline archive deliverer** (`deliver/archive/`) — static, zero-network
  searchable archive openable from `file://`: plain folders, per-encounter
  HTML, rendered PDFs, and FHIR R4 Bundle JSON per patient — readable for
  decades without a database. (#10)
- **Per-patient bundle deliverer** (`deliver/bundle/`) — chart bundles for
  record requests, with per-patient sliced QA reports. (#10)
- **CLI** (`anast`, alias `anastomosis`) — `anast pipeline run <export-dir>
  --out <dir>` with source auto-detection, `--pack`/`--pack-dir`, section
  flag overrides, and `--force`; `anast info` lists available sources and
  packs. (#1)
- CI across ubuntu + windows × Python 3.11/3.12 with a dedicated PHI-scan
  lane and an e2e lane. (#1)

### Security

- **PHI scanner** (`tools/phi_scan.py`) — full-tree scan with a SHA-256
  hashed deny-list and generic PHI patterns, running in pre-commit and CI
  from the first commit; untracked-file blind spot closed; allowlist ledger
  requires written justification per entry. (#1, #9)
- **Log redaction** (`core/logutil.py`) — a logging filter scrubs
  SSN/phone/email/date shapes; error paths log counts, ids, and exception
  type names via `exc_tag()`, never patient-derived values. (#1)
- **Output hygiene** (`core/output.py`) — output directories created `0o700`
  with a PHI-warning README. (#1)
- **Hardened XML parsing** — the C-CDA parser disables entity resolution,
  network access, DTD loading, and huge trees
  (`resolve_entities=False, no_network=True, load_dtd=False,
  huge_tree=False`). (#9)
- **Pack trust model v1** — built-in packs are implicitly trusted; external
  packs load only with explicit `--pack-dir` opt-in. (#1)
- Strict gates: `mypy --strict`, ruff with bandit (S) and naive-datetime
  (DTZ) rules, gitleaks pre-commit, least-privilege CI permissions. (#1)
- `SECURITY.md` — reporting policy, threat model, and security posture. (#9)

[Unreleased]: https://github.com/AzalDaniel/Anastomosis/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AzalDaniel/Anastomosis/releases/tag/v0.1.0
