# Offline OCR decision for image-only clinical PDFs

**Bounded decision record (PHI-free; checked 2026-08-30)**

This record answers one narrow question: what offline OCR/layout fallback should
be used when a clinical PDF page is image-only? It does not authorize a package
installation or a source-code change. No patient content, local fixture, sample
filename, or screenshot was opened for this review.

## Decision

Use a staged pipeline:

1. **Native-text probe, per page and per region.** Try the existing PDF
   text/geometry readers first. Inventory native text objects and embedded-image
   regions separately, and classify each page as native-only, mixed, image-only,
   or ambiguous. If a region contains a usable text layer, keep its native
   coordinates and font/color metadata; do not OCR it merely to obtain a
   prettier representation. A page with some selectable text is not thereby
   safe to treat as fully native.
2. **Default offline fallback: Tesseract 5 CLI.** Render one bounded page image
   at a fixed resolution and run a pinned `tesseract.exe` with fixed language
   data, page-segmentation settings, and `OMP_THREAD_LIMIT=1`. Request both
   TSV and hOCR. Parse words, line/block hierarchy, pixel boxes, confidence
   fields, and orientation into a protected OCR observation sidecar.
3. **Optional second pass: RapidOCR with ONNX Runtime CPU.** Enable only when
   the Tesseract observation fails the configured review/coverage gate or when
   polygonal text detection is needed. Vendor the exact RapidOCR package,
   ONNX Runtime version, ONNX model files, configuration, and SHA-256 hashes;
   disable runtime downloads.
4. **Optional heavy document-layout pass: Docling or PaddleOCR
   PP-StructureV3.** Use only for reviewed use cases requiring region classes,
   table cells, or multi-column reading order. Keep this in a separate optional
   environment because it adds layout models, larger dependencies, and more
   version/model surface area than the default OCR worker.

Windows.Media.Ocr is not the default: Microsoft documents its WinRT OCR
classes as requiring package identity/not being supported in ordinary desktop
apps, its result model has no documented confidence value, and languages are
device language-pack state. PyMuPDF OCR is an optional adapter only where an
AGPL-3.0/commercial Artifex license has been approved; it calls Tesseract, so it
does not by itself add an OCR model or guarantee better recognition.

This is a recommendation for **layout evidence and review triage**, not a
clinical extraction guarantee. OCR text must never be silently promoted to
structured clinical truth.

## Evidence and comparison

| Option | Official evidence, outputs, and coordinates | Offline/deterministic deployment | Limits, style, and licensing |
|---|---|---|---|
| **Tesseract CLI (default)** | [Tesseract installation/user guide](https://tesseract-ocr.github.io/tessdoc/Installation.html) documents the Apache-2.0 engine and hOCR coordinates. [CLI usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html) documents TSV rows with hierarchy (`page_num`, `block_num`, `par_num`, `line_num`, `word_num`), pixel `left/top/width/height`, `conf`, and text; hOCR includes `bbox` and `x_wconf`. [API example](https://github.com/tesseract-ocr/tessdoc/blob/main/APIExample.md) confirms word confidence and bounding boxes. | Windows installers exist for Tesseract 4/5; package the executable and explicit `tessdata` directory. Pin the exact binary hash, traineddata hash, language list, config, input pixel dimensions, and process environment. The engine is CLI-friendly and can run in a minimal offline worker. | TSV/hOCR give text geometry and a score, not source CSS or fonts. Optional hOCR font fields are recognition-derived; the Tesseract man page says LSTM font names may be less precise. `tessdata` and `tessdata_best` are Apache-2.0 repositories; Tesseract uses Leptonica (BSD-2-Clause). Tesseract and model outputs are “as is”; confidence is a triage signal, not a calibrated clinical probability. |
| **PyMuPDF OCR integration** | [PyMuPDF OCR recipe](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html) and [`Page.get_textpage_ocr`](https://pymupdf.readthedocs.io/en/latest/page.html) say PyMuPDF invokes separately installed Tesseract, can OCR a full page or image areas, and returns a reusable `TextPage`. `dpi`, language, full/partial mode, and `tessdata` are explicit. Coordinates returned by PyMuPDF are in the unrotated page coordinate system. | Convenient Python API and wheels for Windows/Linux/macOS; Tesseract language data still has to be supplied. Pin both PyMuPDF/MuPDF and Tesseract. The OCR text layer uses the same Tesseract dependency as the CLI, so cross-platform output is only comparable under pinned rasterizer/engine/model conditions. | Official docs state full-page OCR text uses Tesseract’s `GlyphLessFont`, regular black text; it does not preserve original bold/italic/font. OCR is roughly much slower than native extraction, and Tesseract does not recognize vector graphics as such. PyMuPDF is dual-licensed: open-source AGPL-3.0 or separate commercial Artifex terms ([official repository/license](https://github.com/pymupdf/PyMuPDF)); it is not a safe default for a proprietary package without legal approval. |
| **Windows.Media.Ocr** | Microsoft’s [OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine?view=winrt-28000) returns `OcrResult` split into lines and words. [`OcrWord.BoundingRect`](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrword.boundingrect?view=winrt-26100) is a pixel rectangle measured from the image’s top-left corner. [`OcrResult`](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrresult?view=winrt-28000) documents text, lines, and text angle; no confidence property is documented. | OS-provided WinRT engine, not a cross-platform Python wheel or a vendored model. [AvailableRecognizerLanguages](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.availablerecognizerlanguages?view=winrt-26100) requires the language pack to be installed on the device; [MaxImageDimension](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.maximagedimension?view=winrt-28000) is queried at runtime. Exact behavior follows the Windows build/language-pack image, so record those and do not promise model reproducibility. | [Microsoft’s desktop WinRT restrictions](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/winrt-api-desktop-app-support) (last updated 2026-07-22 when checked) lists `Windows.Media.Ocr.OcrEngine`, `OcrLine`, `OcrResult`, and `OcrWord` among APIs requiring package identity/not supported in ordinary desktop apps. No font/style metadata or confidence is exposed. Use only in an intentionally packaged, Windows-specific helper after runtime validation; Microsoft platform/OS terms apply rather than an OSS model license. |
| **RapidOCR + ONNX Runtime** | [RapidOCR README](https://github.com/RapidAI/RapidOCR) documents `pip install rapidocr onnxruntime`, cross-language deployment, and Apache-2.0 project code. [Official result docs](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/) expose `boxes` (four-point text-line polygons), `txts`, `scores`, per-stage elapsed time, and optional word boxes. [Configuration](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/config.yaml) exposes score thresholds, max side length, word-box flags, and ONNX thread/memory settings. | `rapidocr` v3 packages default models/config; [official model list](https://rapidai.github.io/RapidOCRDocs/main/model_list/) says models can be selected and pre-downloaded, and [offline download guidance](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/) documents `rapidocr download_models`. For a sealed worker, copy exact ONNX files into a read-only model root, set local paths, verify hashes, and block network. [ONNX Runtime thread controls](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) allow explicit intra/inter-op counts and sequential execution. | Polygons/scores are useful layout evidence, but no original font/style is recovered. RapidOCR code is Apache-2.0, while its README says OCR model copyright is held by Baidu; model terms are separate and must travel with the chosen model manifest. ONNX Runtime is MIT ([license](https://github.com/microsoft/onnxruntime/blob/main/LICENSE)); transitive OpenCV/Paddle/model licenses need inventory. ML scores are not clinical probabilities. |
| **Docling** | [Docling installation](https://docling-project.github.io/docling/getting_started/installation/) lists selectable OCR engines and a RapidOCR/ONNX extra. [OCR concepts](https://docling-project.github.io/docling/concepts/OCR/) records RapidOCR versions/backends supported by the project. [PDF pipeline options](https://docling-project.github.io/docling/reference/pipeline_options/) expose `do_ocr`, `force_full_page_ocr`, language, and image `scale`; the docs warn OCR increases processing time. Docling returns structured document/layout representations, rather than a vendor UI. | Code is MIT and can run locally; the current project packaging observed in its official `pyproject.toml` is version 2.117.0 with Python `>=3.10,<4.0` (pin an exact release/commit rather than `main`). Use selective `docling-slim` extras, pre-seed model cache, and disable remote/model downloads. The default `docling`/standard bundle includes substantial local-model dependencies; it is not a lightweight baseline. | Layout/table/reading-order models add CPU/RAM/time and model-specific variability. Docling code license does not grant model/data licenses; inspect each model and dataset separately. It is suitable as an optional layout adjudicator, not as a silent clinical parser or a font/style oracle. |
| **PaddleOCR / PP-StructureV3** | [PP-StructureV3 official guide](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html) describes layout detection, general OCR, optional tables, reading order, and JSON output. It exposes layout regions and `overall_ocr_res` including text polygons, detection scores, recognized text, recognition scores, and table cell boxes. The guide lists 20 common layout categories and says CPU is supported but GPU is recommended for best performance. | Current official repository release is v3.7.0 (2026-06-11 when checked). CPU controls include `cpu_threads`, `enable_mkldnn`, `mkldnn_cache_capacity`, `precision`, and explicit model directories; `predict_iter()` can stream results. Set model directories to local files and block downloads. ONNX Runtime is an available engine for supported modules, but the guide notes some modules may need disabling (for example formula recognition in its ONNX example). | Apache-2.0 project code ([repository](https://github.com/PaddlePaddle/PaddleOCR)); model/data and Paddle/PaddleX dependency terms are separate. PP-StructureV3 is more capable and heavier than a Tesseract/RapidOCR worker; it may infer region labels/reading order incorrectly and does not recover original font/CSS. Use only behind explicit, reviewed layout tests. |
| **pdfplumber** | [Official README](https://github.com/jsvine/pdfplumber/blob/stable/README.md) says it works best on machine-generated rather than scanned PDFs, provides per-character/word/shape coordinates and visual debugging, and is MIT. | Pure Python package, low operational footprint for native PDF inspection. It is suitable for the native-text probe and for comparing a post-OCR text layer, not for recognizing an image-only page. | The README lists OCR as a feature it does not provide. It cannot recognize text that is only pixels; adding a searchable layer requires a separate OCR engine. Native font/color/geometry do not exist for a scan, and OCR-injected text must be treated as synthetic. |
| **pdfminer.six** | [Official README](https://github.com/pdfminer/pdfminer.six/blob/master/README.md) says it extracts text directly from PDF source code, including exact location/font/color, with automatic layout analysis. The [project metadata](https://github.com/pdfminer/pdfminer.six/blob/master/pyproject.toml) is MIT and currently requires Python `>=3.10`. | Pure Python and useful for a platform-neutral native-text probe. It does not supply image recognition, page rasterization, or a calibrated confidence stream. | An image-only page has no source text objects for pdfminer to recover. It may inspect embedded images, but it cannot turn pixels into clinical text without a separate OCR backend. Its font/location evidence applies to real PDF text objects, not to OCR guesses. |

## Why the default is Tesseract CLI, not a visual guarantee

Tesseract gives the smallest auditable surface that satisfies the fallback
need: a locally runnable binary, explicit language data, standard TSV/hOCR
coordinates, word confidence, and no Python binding or proprietary runtime
requirement. Its output is easy to retain as an observation sidecar and to
replay in a fixed worker.

The Tesseract [benchmark guidance](https://tesseract-ocr.github.io/tessdoc/Benchmarks.html)
and [FAQ](https://tesseract-ocr.github.io/tessdoc/FAQ.html) recommend explicit
`OMP_THREAD_LIMIT` control; the FAQ notes that setting it to `1` disables
multithreading, while the benchmark page discusses single-threaded operation
for controlled/mass processing. Use one page per process or a very small,
explicit worker pool. A timeout and OS-level process/job boundary remain
necessary because the CLI has no application-level memory quota.

Tesseract’s optional hOCR font fields and confidence do not reconstruct the
scanned document’s original font, weight, CSS, or EHR renderer. PyMuPDF’s
official OCR documentation makes the limitation concrete: generated text is
written with `GlyphLessFont`, regular and black. A layout pack may use the
pixel geometry, line/border evidence, and reviewed destination fonts, but it
must not assert that OCR recovered the original rendering system.

## Mixed pages: raster plus some native text

Clinical PDFs are often neither purely machine-generated nor purely scanned.
A page can contain a native header and a rasterized note body, a scanned form
with native annotations, a raster image with a pre-existing hidden OCR layer,
or native vector/text overlays on top of an embedded scan. The worker must
handle that as a region-provenance problem, not as a binary page switch.

The mixed-page policy is:

1. Build a page inventory before OCR. For each text object and image/paint
   region, record its page-space bounding box, source kind (`native_text`,
   `raster`, or `ambiguous`), and the reader/rasterizer evidence used. Native
   text objects include a source object identifier and remain an independent
   evidence stream. A selectable text layer may itself be synthetic; if it
   overlaps a scan or has suspicious coverage, mark it `ambiguous` rather than
   treating it as clinical truth.
2. OCR only raster or ambiguous regions when their page/image transform is
   known. Crop or mask the region deterministically, retain the parent page
   image hash, and map pixel polygons back to the one named PDF/page coordinate
   frame. Keep a `region_id` and transform hash on every OCR observation. Do
   not OCR a native region just because the page also has a scan.
3. Preserve native and OCR streams separately. Native tokens, font metadata,
   and geometry are not replaced by OCR observations. If region isolation is
   not reliable, use a full-page OCR sidecar only as a fallback and deduplicate
   it against native objects by explicit geometric overlap plus normalized text;
   never silently choose one value when they disagree. A duplicate or conflict
   becomes a review item, and the canonical semantic IR/source attachment is
   unchanged.
4. For layout learning, native geometry can inform destination style only with
   its provenance intact. OCR geometry can suggest line/column/table regions,
   spacing, and page breaks, but OCR text cannot fill a missing semantic field.
   For migration, a native value remains source evidence; an OCR-only value is
   unverified unless an independent structured source or reviewer confirms it.
5. If the page has a pre-existing OCR text layer, retain its text objects as
   `native_or_synthetic` evidence and compare them with the raster. Do not
   assume that a searchable PDF layer is accurate merely because text
   extraction succeeds. If the layer conflicts with a new OCR observation,
   preserve both and hold the page for review.

This policy also prevents a common failure mode in which a full-page OCR pass
duplicates selectable labels, headings, or annotations and then shifts reading
order. Region-level OCR is preferred; full-page OCR is permitted only when
coverage/transform checks show that region isolation is unavailable, and its
result must be marked `full_page_fallback` in the observation manifest.

### Mixed-page acceptance fixtures and gates

The reviewed synthetic/de-identified fixture pack must include at least:

- native header plus raster body;
- raster form plus native annotation/date overlay;
- an embedded scan with a hidden OCR text layer;
- native text and vector rules over a raster background; and
- a page where the native layer covers only a small label or footer.

For each fixture, the gate requires that native object count, text, geometry,
and font/color metadata are unchanged; raster/ambiguous regions have an
explicit OCR decision; no accepted OCR token duplicates a native token; and
every native/OCR overlap or text disagreement is counted and routed to review.
The page must fail closed if its region transform is outside the declared
coordinate bounds, if raster coverage is unknown, or if a full-page fallback
would make provenance ambiguous. A mixed page with partial native text is
therefore never silently downgraded to “native-only” and never silently
upgraded to “OCR-verified.”

## Dependency and packaging implications

### Baseline Windows VM and cross-platform package

The baseline should be a separate offline worker, with a small adapter in the
main Python package:

- **Required worker assets:** exact Tesseract executable, `tessdata` language
  files, a fixed config file (`psm`, language, output formats), and a fixed
  page rasterizer. Record SHA-256 for each. The executable path and
  `TESSDATA_PREFIX` must be explicit; do not depend on a user's PATH or a
  mutable system tessdata directory.
- **PDF rasterization:** feed Tesseract a page image. Prefer a renderer
  already approved by the repository. If none exists, [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)
  is a possible permissive option: its project documents Apache-2.0/BSD-3-Clause
  bindings, PDFium’s BSD-style license, and the need to ship PDFium dependency
  notices. Do not silently add it; perform the normal dependency/license
  review first. PyMuPDF is not an equivalent license substitute.
- **Python packages:** parsing TSV/hOCR needs only the standard library plus
  the package’s existing image/geometry utilities. Avoid `pytesseract` as a
  required dependency unless its own wrapper behavior is needed; the CLI
  contract is easier to pin and audit. If a wrapper is used, retain the exact
  executable invocation in the manifest.
- **Windows:** test the same immutable worker directory on the authorized
  Windows VM. Capture Windows build, CPU architecture, Tesseract version,
  language-pack hashes, rasterizer version, and input image dimensions. Do not
  claim byte-identical output with Linux merely because both use Tesseract;
  maintain per-platform golden baselines or use a common container/binary.
- **Linux/macOS package:** keep the same adapter and output schema. Ship a
  platform-specific pinned Tesseract binary/model bundle or require an
  operator-provided binary whose version/hash is checked at startup. Refuse to
  run when the hash, language, or model is not in the allow-list.

### Optional RapidOCR/ONNX extra

Keep RapidOCR out of the baseline environment. An optional extra should pin:

```text
rapidocr==<reviewed-version>
onnxruntime==<reviewed-version>
<exact det/cls/rec ONNX model files and hashes>
<exact RapidOCR YAML and character dictionary hashes>
```

The placeholder versions are intentional: choose and test a release, then
lock it in the repository’s normal dependency mechanism. The official RapidOCR
docs show the package/model version axes and local model-root/config controls;
the implementation must not install “latest” or download from ModelScope at
runtime. ONNX Runtime’s CPU provider is the cross-platform baseline; do not
enable CUDA, DirectML, OpenVINO, or another execution provider in the default
worker because provider selection and kernels can change results and add
runtime DLL/license obligations.

### Optional Docling/Paddle layout environment

Install neither in the default worker. Use a separate, locked environment:

- Docling: choose `docling-slim[format-pdf,feat-ocr-rapidocr]` or the exact
  reviewed extra set from the project’s [official packaging table](https://docling-project.github.io/docling/getting_started/installation/),
  not the batteries-included bundle unless layout/table models are required.
  The official project currently advertises version 2.117.0 in its package
  metadata; pin the exact release/commit and model revisions. Record every
  model/data license separately.
- PaddleOCR: pin the exact `paddleocr`, Paddle/PaddleX, and model directories;
  set CPU device, `precision=fp32`, a fixed `cpu_threads`, and disable optional
  table/formula/chart modules unless their outputs are in scope. The official
  PP-StructureV3 guide says model directories otherwise may trigger downloads.
  Use only the modules tested on the selected CPU/ONNX path.

This split keeps a routine Windows VM deployment small and makes a heavy
layout model an explicit, reviewable capability rather than an accidental
transitive dependency.

## Resource-control profile

The numbers below are a starter engineering profile, not an engine guarantee;
benchmark and adjust on the authorized VM using synthetic/de-identified pages.
The key requirement is that every limit is finite, recorded, and enforced.

| Control | Default worker | RapidOCR/ONNX escalation | Docling/Paddle optional worker |
|---|---|---|---|
| Page raster | Fixed 216 DPI (3 x 72-point PDF units) or an explicitly approved fixed profile; cap total pixels before OCR | Same page image whenever comparing engines; do not let each engine resample differently | Fixed pipeline scale/DPI; record `scale`, page image hash, and model preprocessing |
| Pixel cap | Start at 25 million pixels/page; reject or downsample through a recorded deterministic rule | Same cap; fail closed on unsupported dimensions | Same cap unless the approved model profile documents a different cap |
| Concurrency | One page/process; `OMP_THREAD_LIMIT=1`; bounded queue | One ONNX session; `intra_op_num_threads=1`, `inter_op_num_threads=1`, sequential execution | `cpu_threads=1` initially; no multiprocess fan-out until RSS is measured |
| Time | 60 seconds/page and a finite job deadline are reasonable starting gates | Separate detection/recognition timings; same 60-second page deadline initially | Longer explicit budget allowed only in the optional profile; timeout still mandatory |
| Memory | Enforce a worker/job RSS limit at the OS boundary; delete page intermediates on completion | Set `enable_cpu_mem_arena` deliberately and record it; cap model/session count | Use page-range/iterator processing, disable unused modules, and record peak RSS |
| Network | No egress; no model/font/ language auto-download | No egress; local model root and SHA-256 allow-list | No egress; local model/cache directories and allow-list |
| Failure | Timeout, dimension, language, subprocess, or hash failure produces a review item, never partial silent success | Preserve both pass/fail and engine disagreement | Heavy backend failures cannot cause fallback to unreviewed semantic extraction |

For ONNX Runtime, the official [thread-management guide](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)
says the default CPU thread count can follow physical cores and documents
explicit intra/inter-op controls and sequential execution. Explicit values are
therefore required for repeatability and to prevent a VM-wide thread storm.
PaddleOCR documents equivalent CPU-thread and MKL-DNN controls in its
[PP-StructureV3 guide](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html).

## Small backend interface

The adapter should expose a stable, engine-neutral observation schema. This is
an interface design for a future implementation, not source code to apply in
this decision record.

```text
PageImage {
  page_index: int
  bytes_sha256: string
  width_px: int
  height_px: int
  dpi_x: float
  dpi_y: float
  rotation_deg: float
  page_width_pt: float
  page_height_pt: float
}

OCRConfig {
  backend_id: "tesseract-cli" | "rapidocr-onnx" | "windows-media-ocr" |
              "docling" | "paddle-pp-structure-v3"
  language: string
  model_manifest_sha256: string
  page_segmentation: string | null
  max_pixels: int
  timeout_seconds: int
  thread_count: int
  allow_network: false
}

OCRToken {
  text: string                 # protected observation; never put in audit log
  polygon_px: [[float,float], ...]
  bbox_px: [float,float,float,float]
  bbox_page_pt: [float,float,float,float]
  line_id: string | null
  block_id: string | null
  confidence: float | null
  confidence_kind: string | null # e.g. tesseract_conf, rapidocr_score, none
  source: "ocr_observation"
}

OCRPageResult {
  tokens: [OCRToken, ...]
  regions: [layout-region, ...]
  warnings: [string, ...]
  engine_version: string
  model_manifest_sha256: string
  config_sha256: string
  input_page_sha256: string
  timings_ms: {render, detect, recognize, total}
}
```

Implementation rules:

- Preserve every raw token, including low-score/empty tokens, in protected
  output. A threshold may select layout candidates, but it must not silently
  delete evidence. Record the threshold and excluded-token count.
- Normalize all coordinates to a named frame. Tesseract hOCR/TSV and
  Windows.Media.Ocr use image pixels; RapidOCR/Paddle use image polygons;
  pdfplumber/pdfminer/PyMuPDF native page geometry uses PDF/page units. Store
  image dimensions, DPI, rotation, crop box, and the transform used to map to
  page points. Do not mix top-left image coordinates with unrotated PDF
  coordinates without an explicit transform.
- `confidence` is nullable. Windows.Media.Ocr has no documented score, so use
  `null`, not a fabricated value. Do not compare scores from different engines
  as though they share a calibration.
- OCR text is an observation attached to an image hash. The semantic IR may
  reference a reviewed OCR observation, but an engine cannot invent, correct,
  or normalize a medication, allergy, result, date, identity, or clinician.
- Keep source bytes/hash and the rendered page hash. A generated searchable PDF
  or reflowed preview must say that the text layer is OCR-derived.

## Safe use for layout learning and migration

OCR is permitted to suggest:

- text-line/word boxes, block adjacency, columns, page regions, repeated header
  or footer bands, table candidates, and reading-order hypotheses;
- rough line-height, spacing, alignment, border/line presence, and page-break
  evidence from the image;
- a reviewer queue ordered by engine score, disagreement, or geometry anomaly.

OCR is **not** permitted to establish:

- that a token is clinically correct, complete, or semantically mapped;
- that an observed font, color, line weight, CSS rule, or page image is the
  destination EHR’s native rendering;
- that the first sample generalizes to optional fields, long notes, scans with
  stamps/handwriting, pagination, tenant configuration, or future releases;
- that a higher engine score means higher clinical reliability.

For high-risk content (identity, dates, author, medications, allergies,
results, status), automatic migration must either have an independent
structured source or require reviewer confirmation against the source image.
When no independent source exists, preserve the image attachment and mark the
text “OCR-derived; not semantically verified”; do not silently write it into a
structured destination field. Derived layout tokens have provenance
`ocr_observation`, not source clinical provenance.

## Acceptance gates

These are proposed local gates and must be calibrated on synthetic or
de-identified, manually reviewed fixtures. They are not guarantees made by
Tesseract, Microsoft, RapidAI, Docling, PaddleOCR, ONC, HL7, or a vendor.

1. **Page and region classification:** the native-text probe records why OCR
   was or was not needed for every region. It distinguishes native-only,
   mixed, image-only, and ambiguous pages; a page with some native text is not
   automatically exempt from OCR. Image-only and mixed pages retain the source
   PDF/page SHA-256, region map, and rendered page dimensions. No OCR worker is
   invoked without a finite pixel/page limit or a deterministic region-to-page
   transform.
2. **Manifest integrity:** every result records backend, binary/package version,
   model/data/config hashes, OS/architecture, language, DPI, rasterizer, thread
   settings, and timeout. Missing or mismatched hashes block publication.
3. **Coordinate integrity:** all tokens have a valid polygon/bbox inside the
   image bounds and a deterministic page-coordinate transform. On the reviewed
   geometry fixture set, median word-box IoU is at least 0.90 and no critical
   region is clipped or outside the page. Use a stricter threshold only when the
   approved fixture pack supports it.
4. **Repeatability:** run each approved page three times in the same sealed
   environment. Normalized token order, boxes, warnings, and metadata are
   identical; if an engine is intentionally nondeterministic, the page is not
   eligible for automatic style learning. Cross-OS runs are compared against
   separate platform baselines, never promised byte-for-byte equivalent.
5. **Critical-field safety:** zero OCR-derived high-risk values are promoted
   automatically without independent structured evidence or reviewer sign-off.
   A single disagreement, low-confidence critical region, missing language, or
   timeout holds the page. “100% accurate” is prohibited; report exact-match
   results only for the named fixture pack.
6. **No evidence loss:** raw token count, low-score count, discarded-candidate
   count, and engine-disagreement count are reported. Zero unexplained drops;
   every filtered token has a reason. Unsupported/ambiguous regions enter the
   review queue.
7. **Resource envelope:** the worker test passes the configured pixel cap,
   page/job deadline, RSS limit, and concurrency limit on the target VM. A
   limit breach kills/holds the page and leaves a PHI-free aggregate error
   record; it does not return a partial “successful” migration.
8. **Visual/layout evidence:** a learned style pack stores geometry, region
   classes, reading-order hypotheses, and asset hashes only. Human approval is
   required on short/long/empty/multiline/table/attachment/page-break fixtures.
   Visual similarity is measured separately from semantic fidelity and cannot
   waive a clinical-field or provenance failure.
9. **Offline/security:** runtime network calls are zero; model/config hashes
   verify; temporary page images are bounded and removed according to the
   worker policy; logs contain no patient values, OCR text, original filenames,
   or screenshot pixels.
10. **Toggle isolation:** changing the Migration toggle or OCR/layout backend
    does not change the canonical semantic IR hash or source attachment hash;
    only the observation/style/render manifest changes. The output identifies
    the OCR-derived layer and says it is not native destination-EHR UI.

## Explicit non-decisions

- No OCR engine is approved as a clinical decision-maker, medical coding
  system, or evidence that a migration is complete.
- No Windows-only engine is added to the cross-platform package merely because
  it is installed on one VM.
- No AGPL/commercial dependency is added without a license decision.
- No model is downloaded at runtime, and no “latest” tag is acceptable in a
  reproducible migration run.
- No OCR screenshot, marketing sample, or proprietary EHR asset is a template
  conformance oracle.

## Source index (official/primary)

Tesseract: [engine repository](https://github.com/tesseract-ocr/tesseract),
[installation](https://tesseract-ocr.github.io/tessdoc/Installation.html),
[CLI/TSV/hOCR](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html),
[API confidence/boxes](https://github.com/tesseract-ocr/tessdoc/blob/main/APIExample.md),
[thread benchmark](https://tesseract-ocr.github.io/tessdoc/Benchmarks.html),
[tessdata](https://github.com/tesseract-ocr/tessdata), and
[tessdata_best](https://github.com/tesseract-ocr/tessdata_best).

PyMuPDF: [OCR recipe](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html),
[`get_textpage_ocr`](https://pymupdf.readthedocs.io/en/latest/page.html),
[installation/wheels/Tesseract data](https://pymupdf.readthedocs.io/en/latest/installation.html),
and [license](https://github.com/pymupdf/PyMuPDF).

Microsoft: [OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine?view=winrt-28000),
[OcrResult](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrresult?view=winrt-28000),
[OcrWord bounding rectangle](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrword.boundingrect?view=winrt-26100),
[available languages](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.availablerecognizerlanguages?view=winrt-26100),
[maximum image dimension](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.maximagedimension?view=winrt-28000),
and [desktop WinRT restrictions](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/winrt-api-desktop-app-support).

RapidOCR/ONNX: [RapidOCR repository](https://github.com/RapidAI/RapidOCR),
[result usage](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/),
[parameters](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/parameters/),
[model list](https://rapidai.github.io/RapidOCRDocs/main/model_list/),
[offline model download](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/),
[ONNX Runtime license](https://github.com/microsoft/onnxruntime/blob/main/LICENSE),
[ONNX Runtime threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html),
and [ONNX Runtime installation/providers](https://onnxruntime.ai/docs/install/).

Docling/Paddle: [Docling installation](https://docling-project.github.io/docling/getting_started/installation/),
[Docling OCR concepts](https://docling-project.github.io/docling/concepts/OCR/),
[Docling pipeline options](https://docling-project.github.io/docling/reference/pipeline_options/),
[Docling repository](https://github.com/docling-project/docling),
[PaddleOCR repository/license](https://github.com/PaddlePaddle/PaddleOCR),
[PP-StructureV3 guide](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html),
and [PaddleOCR package metadata](https://github.com/PaddlePaddle/PaddleOCR/blob/main/pyproject.toml).

Native PDF readers/rasterizer: [pdfplumber](https://github.com/jsvine/pdfplumber/blob/stable/README.md),
[pdfminer.six](https://github.com/pdfminer/pdfminer.six/blob/master/README.md),
[pypdfium2](https://github.com/pypdfium2-team/pypdfium2), and
[pypdfium2 license inventory](https://github.com/pypdfium2-team/pypdfium2/blob/main/REUSE.toml).
