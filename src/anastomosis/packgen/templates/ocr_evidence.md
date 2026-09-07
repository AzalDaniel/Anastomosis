# OCR evidence for DRAFT pack: ${name}

${caveat}

## What this pack may be used for

Recognized geometry MAY suggest: text-line and word boxes, block adjacency,
columns, repeated header/footer bands, table candidates, spacing, and
page-break evidence.

Recognized text MAY NOT establish: that a value is clinically correct or
complete; that an observed font, weight, color or page image is the source
system's own rendering; or that a higher engine score means higher clinical
reliability. Tesseract writes its text layer glyphless and black — no face,
weight or color survives recognition, so this draft's typography is a
destination choice, not a recovered one.

## Page provenance

${classes}

## Observation counts

- Tokens returned by the engine: ${token_count}
- Used as layout evidence: ${accepted_count}
- Below the confidence threshold (retained as a count, not promoted):
  ${below_count}
- Duplicates of native text (dropped from the layout candidates, counted here):
  ${duplicate_count}
- Native/OCR disagreements (BOTH kept; nothing was resolved):
  ${disagreement_count}

## Held conflicts

Nothing below was resolved. Where the two streams described the same place, the
native object and the recognized token were both kept and the page was held for
review. Boxes are in PDF points; no text appears here by design.

${conflicts}

## Engine manifest

${manifest}
