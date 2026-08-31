# The run manifest — profiles, binding, and state

Reference for `core/profiles.py` and `core/runmanifest.py`. The modules'
docstrings state the invariants; this is the file's field list and the refusal
table, which a reader needs once.

## Why the file exists

A migration's artifacts mean something only in terms of the three things that
made them: the **source** that read the export, the **destination** they were
shaped for, and the **layout** that rendered the pages. All three can change
underneath an operator between one command and the next — a learned mapping is
hand-editable, a template pack is a directory of files, a destination entry is
re-verified data. Nothing on disk recorded which versions of them a folder's
artifacts came from, so nothing could refuse when they moved.

`run_manifest.json` records it, beside `charts/` and `ccda/` in the run's own
output directory. A later step over the same folder — re-running the migration,
uploading from it, recording a delivery — recaptures the three profiles from the
machine as it now stands and compares hash to hash.

## The three profiles

Each is a frozen dataclass addressed by SHA-256 over its own canonical JSON
(`sort_keys`, tight separators, ASCII), domain-separated by schema version and
profile kind. Nothing is hashed twice: where a digest already existed, the
profile carries that one.

| Profile | Fields in the address | Digest it reuses |
| --- | --- | --- |
| `SourceProfile` | `name`, `kind` (`builtin`/`learned`), `mapping_id`, `mapping_sha256`, `spec_version`, `taught_for_destination`, `taught_for_destination_hash` | the learned mapping's `source_trust.json` digest (`spec.mapping_content_hash`) |
| `DestinationProfile` | `name`, `display`, `version`, and each capability's `slot`/`kind`/`detail` | — (registry data) |
| `LayoutProfile` | `render_mode`, `pack`, `origin`, `content_hash` (`root` recorded, not hashed) | `packtrust.pack_content_hash` |

A built-in source has no content hash: its behavior is the installed package's,
which the manifest pins separately as `pipeline_version`.

Four deliberate exclusions:

* **Capability evidence is not hashed.** The quarterly re-verification ritual
  bumps a `verified` date without changing what a destination can receive; a
  binding that broke every time somebody re-read a vendor doc page is a binding
  people learn to ignore. A changed `kind` or `detail` *is* a changed routing
  fact and does break it.
* **The export's contents are not hashed, and neither is its path written
  down.** `export_dir_id` is a digest of the resolved input PATH and the path
  itself never lands. The digest answers "was this run pointed at the same
  folder?", not "does that folder still hold the same records" — which is the
  conservation ledger's question, and would put patient-derived bytes in reach
  of a manifest. The path is withheld for the same reason: a practice that
  drops one folder per patient names those folders after patients.
* **The layout's `root` is recorded but not hashed.** A later step — an upload,
  a delivery — never received the `--pack-dir` list the migration was given, so
  it cannot rediscover an external pack; the recorded root is where it asks
  whether those bytes still hash the same. A pack moved to another path with
  its bytes intact is not drift, and a machine-specific path inside the digest
  would make every binding unportable.
* **What the layout hash itself omits.** `pack_content_hash` covers
  `context.py`, `template.html` and `pack.yaml`. Auxiliary assets beside them
  are unpinned there and therefore unpinned here — an edited logo or stylesheet
  moves no binding. (`render_provenance.json`, where a run writes one, is the
  record that does cover the whole tree.)

## Destination versions

`DestinationEntry.version` defaults to the explicit string `"unversioned"`,
never a missing key. Every entry the registry ships today declares no version:
these are continuously-updated hosted products with no version an operator can
read off a screen, and asserting one would be a claim about the world with no
evidence behind it — the same no-hallucination rule the capability blocks keep.
An entry for a product that *does* carry one (a self-hosted release line, a
dated API surface) states it, and every run bound to that version refuses when
it changes.

## The file — v1

`RUN_MANIFEST_VERSION` is 1. There is no degraded read: a version this build
does not know cannot be compared against, and comparing wrongly is worse than
refusing.

| Field | Scope | What it carries |
| --- | --- | --- |
| `version` | file | The manifest schema version. |
| `pipeline_version` | run | The Anastomosis version that prepared the artifacts — the identity of every built-in source and renderer involved. |
| `state` | run | `prepared`, `delivered`, or `verified`. |
| `state_history` | run | The ordered states this run has been in. |
| `receipt` | run | A PHI-free pointer to the evidence behind the current state (an upload run-report name). `null` while `prepared`, whose evidence is the artifacts themselves. |
| `run.source` / `run.destination` / `run.render_mode` | run | The identities a later step re-profiles. |
| `run.export_dir_id` | run | A digest of the operator-chosen input path. Never the path, never contents. |
| `profiles.source` / `.destination` / `.layout` | run | Each profile's full payload plus its own `profile_hash`. |
| `profiles.binding_hash` | run | The digest over the three profile hashes. |

Every recorded hash is read back on load and compared against what the
manifest's own contents produce; a file that does not agree with itself is
refused rather than believed. That is an integrity check against a hand edit
and a half-written file — **not** a security boundary: nothing here is signed,
and anyone who can edit the file can recompute these too. The controls that
carry weight are the trust store and the profiles themselves.

**No timestamps.** Two runs over the same inputs on the same pinned environment
write byte-identical manifests, which is what makes "did anything change?" a
comparison rather than a judgement — the same rule `upload_manifest.json` keeps.
When a transition happened is the artifacts' own mtimes.

**PHI:** hashes, names, counts, versions, state names, and operator-chosen
paths. Nothing read out of the export ever reaches this file.

## Where a binding is checked, and what happens

| Step | On drift | On no manifest |
| --- | --- | --- |
| `anast migrate` into a folder that already has one | Refuses (`PipelineError`, kind `binding_changed`, exit 2) naming which profile moved. `--rebind` prepares the folder again under the current profiles. | Prepares and writes one. |
| `anast source init --to X`, then migrating that source to Y | Refuses (kind `destination_mismatch`, exit 2) naming both destinations — or, at the same destination after it changed, naming both hashes. | — |
| `anast upload` | Refuses (`BindingError`) before the browser is touched. | Uploads as before, with one PHI-free warning that nothing was checked. |
| `runmanifest.advance_state` | Refuses (`BindingError`). | Refuses (`BindingError`) — a state cannot be recorded against inputs nobody wrote down. |

A manifest that is *present but unreadable* always raises: "never bound" and
"bound, and we cannot tell to what" are different answers.

## The state machine

```
prepared ──> delivered ──> verified
```

`migrate` writes `prepared` and nothing else: it resolves a route and writes
artifacts, and executes no delivery — the invariant `core/migration_status.py`
states, unchanged. A state past `prepared` is a claim that something happened,
so `advance_state` requires a `receipt` naming the evidence. Today the one
producer of such evidence is a clean `anast upload`, which records `delivered`
(receipt: the run report) and, when the L0–L6 ladder ran, `verified`. Moves
backwards, and the skip from `prepared` straight to `verified`, are refused
(`RunStateError`).
