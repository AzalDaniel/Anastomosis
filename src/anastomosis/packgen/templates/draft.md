# DRAFT pack: ${name}

> ${display}

**This is a DRAFT, not a finished pack.** It was auto-generated from
${sample_count} sample PDF(s) by `anast pack init --from-samples`. The
layout learner recovers roughly 60-70% of a pack deterministically; the rest
is a human's job. **Fidelity to your originals is NOT claimed** — treat the
output as a starting point.

## Same-patient caveat (read this first)

${same_patient_caveat}
${ocr_section}
## Provenance

- Samples analyzed: ${sample_count}
- Confidence: ${confidence}
- Page geometry: ${width}x${height}pt
  (margins L${margin_left} R${margin_right}
  T${margin_top} B${margin_bottom}pt)
- Emitted page size: `${page_size}`${page_size_note}
- Heading-band fill: `${heading_fill}`
- Body font: `${body_font}`
- Dropped curves (vector art the harvester skipped): ${dropped_curves}
- Layout evidence: ${evidence_line}

## Inferred heading sections

${section_lines}

## Sample-text quarantine

${unplaced_note}

${unplaced_lines}

## Next steps

1. **Review side-by-side.** Render a preview (`--render-preview`, or
   `anast pipeline run … --pack ${name} --pack-dir <this dir's parent>` —
   passing `--pack-dir` opts into trusting this draft's code) and compare
   the rendered PDF in `preview/` to an original sample.
2. **Review `UNPLACED.txt`, then edit `template.html`.** Reposition text you
   keep, wire any inferred heading sections into real loops, and adjust the inlined design
   tokens (CSS custom properties in `:root`).
3. **Re-render** and repeat until the preview matches your sample.
