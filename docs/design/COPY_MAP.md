# Copy map — every user-facing string, old → new

Register rules in `DESIGN_LANGUAGE.md` §10. This file is the binding list:
GUI code carries the NEW strings only. Technical ids stay available in
tooltips (`title=`) where marked. Casing: sentence case everywhere; product
and format names keep their proper casing (C-CDA, FHIR).

## Navigation / chrome

| Old | New |
| --- | --- |
| Dashboard | Charts |
| Migration wizard | Migrate |
| Upload console | Uploads |
| Pack from samples · Learn a source (two tabs) | Teach (one view, two modes: “Document layout” / “Export format”) |
| “Anastomosis” in-page title bar + h1 + nav (4×) | (removed — the OS window title carries the name) |
| `<div class="version">—</div>` on every page | About popover: “Anastomosis <version> · AGPL-3.0” |

## Charts (was Dashboard)

| Old | New |
| --- | --- |
| Reconstruct, verify, and re-home clinical records. | Turn an EHR export into complete, verified charts. |
| Export directory (`/path/to/ehi/export`) | Export folder — “The folder your EHR gave you when you exported your records.” placeholder: `C:\Users\you\Downloads\ehr-export` |
| run pipeline | Rebuild charts |
| Source format | Export format — “Which system this export came from. ‘Detect’ works for all built-in formats.” |
| Template pack | Chart layout — “How the finished chart pages are laid out.” |
| Extra pack directory | (Advanced) Additional layout folder |
| qa on / qa off | Double-check results: on / off — “Re-reads every finished chart and confirms names, dates, and values landed on the right patient.” |
| trust new pack code | (Advanced) Allow this new layout to run — “Layouts contain code. Anastomosis refuses layouts it has not seen before unless you allow them once here.” |
| write upload manifest | Prepare for upload — “Also writes the files the Uploads screen needs to file these charts into another system.” |
| force re-render | Rebuild pages even if unchanged |
| stage rail: ingest / reconstruct / qa / deliver | Reading records → Building charts → Double-checking → Saving results |
| “Launch it with `anast gui` (needs the anastomosis[gui] extra).” | “The desktop app is not connected. Close this window and start Anastomosis from the Start menu.” |

## Migrate (was Migration wizard)

| Old | New |
| --- | --- |
| Source → destination → the shortest viable route. | Move charts from one system into another. |
| viable / not viable | available / not available |
| Chart representation (--render) | Chart pages — “Rendered pages (PDF) or data only.” |
| Browser pack X ready (selectors discovered) | Filing assistant for X is ready |
| …needs discovery — run `anast destination init`… | The filing assistant for this system has not been set up on this computer yet. Set it up from the Teach screen. |
| Run the pipeline with the C-CDA deliverer… `anast pipeline run <export> -o out --ccda` | This route creates a C-CDA transfer document the destination can import. “Rebuild charts” below produces it. |
| API push wiring lives in deliver/fhir_api — credentials required. | This route sends charts directly to the destination’s FHIR interface. It needs sign-in credentials from your destination system, and runs from the Uploads screen. |
| The full API-run UI is part of a later milestone | Direct sending is not available from this screen yet. |
| No viable route… Contribute evidence or re-run the registry re-verification ritual. | No route to this destination is available yet. Routes appear here once they have been verified to work. |
| live API push: later milestone (badge) | (removed) |
| migration prepared — charts + C-CDA payload written; delivery not yet executed. | Charts and the transfer document are written. Nothing has been sent yet — review the results, then continue on the Uploads screen. |

## Uploads (was Upload console)

| Old | New |
| --- | --- |
| Browser-delivery operator surface — inspect a ledger and drive uploads. | Watch charts being filed into the destination, and start or stop the work. |
| CDP endpoint (loopback only, e.g. 127.0.0.1:9222 with an http scheme) | (Advanced) Browser connection — “Anastomosis files charts through a browser window it controls on this computer. Leave this as suggested unless support asks you to change it.” default value pre-filled |
| Press Cmd/Ctrl+K for the item-key palette (encounter id + content hash — never patient names). | Search box, visible: “Find an upload — search by visit id.” |
| Skiplist (optional) — one item key or encounter id per line; “#” comments ignored | (Advanced) Skip these — “One visit id per line. Lines starting with # are notes to yourself and are ignored.” |
| Verify uploads (L0–L6) | Double-check each chart after filing — “Confirms the right chart landed on the right patient before moving on.” |
| needs the render extra and refuses to run without it | (removed — the app either can or cannot; when it cannot: “This installation cannot double-check charts. Reinstall the full package to enable it.”) |
| already-filed items are not re-driven | Charts already filed are left alone. |
| counter “terminal” (green) | Buckets and colors: Filed (green) · Needs attention (red) · In progress (amber) · Waiting (neutral) |
| error type histogram / Exception TYPE names and counts only | What went wrong — kinds of errors and how many. Never patient information. |
| state values shown raw (`pre_verify_failed`…) | Plain labels + tooltip with the technical id: resolving_patient → “Finding the patient” · uploading → “Filing the chart” · pre_verify_failed → “Stopped before filing — the identity check did not pass” · post_verify_failed → “Filed, but the after-check did not pass — needs review” · patient_not_found → “Patient not found in the destination” · duplicate_at_destination → “Already in the destination — left alone” · upload_interrupted → “Interrupted — will resume” · skipped_skiplist → “Skipped at your request” · done → “Filed and confirmed” |

## Teach (was Pack from samples + Learn a source)

| Old | New |
| --- | --- |
| Pack name (lowercase identifier, e.g. acme_soap) | Layout name — “Lowercase letters and underscores, e.g. acme_soap.” |
| inferred design (phi-safe summary) | Proposed layout — no patient data is shown below. |
| fidelity is not claimed | This draft matches the samples’ structure; review the result before relying on it. |
| Teach a new flat export (CSV/TSV/JSON) from one example. | Teach Anastomosis your export format from one example file. |
| Example file (.csv/.tsv/.json/.ndjson), or a dir holding one | Example file (.csv, .tsv, .json) — or the folder holding it |
| proposed mapping (phi-safe — no values shown) | Proposed match-up — no patient data is shown below. |
| canonical field / transform / confidence (headers) | Goes to / How it is read / Confidence |
| (unmapped → extensions) | (kept, unmatched — nothing is dropped) |
| Saving proves the mapping drops no column (round-trip), then writes it owner-only. Refine the saved `mapping.json`… | Saving first proves no column of your file would be lost, then stores the format for your user account only. |
| Refusing to save — these columns would be dropped: … | Cannot save yet — these columns would be lost: … Every column must have a home before the format is saved. |
| format X · N columns · patient key … · row scope … | Prose: “X file · N columns · patients identified by ‹column› · one row per ‹scope›.” |

## Cross-cutting

* “PHI-safe” never appears in copy → “no patient data is shown”.
* “pack” disambiguated everywhere: rendering pack → **chart layout**;
  browser pack → **filing assistant**.
* c-cda / C-cda → **C-CDA** everywhere.
* No exclamation marks. No emoji. No “ritual”, “milestone”, “payload”,
  “operator”, “surface”, “drive/driven”, “ledger” (→ “record of uploads”),
  “manifest” (→ “upload files”), “pipeline” (→ “rebuild”), “render”
  (→ “chart pages”) in visible copy.
* Error strings keep the loud-refusal semantics but in plain language, and
  always say what to do next.
