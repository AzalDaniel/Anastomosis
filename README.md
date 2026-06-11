# Anastomosis

> Reconstruct, verify, and re-home clinical records.

**anastomosis** *(n., medicine)* — a surgical connection between two structures.
This toolkit is that connection for electronic health records.

> 🎥 **Video demo:** *(coming with the CS50 final-project submission)*

## The problem

Every certified US EHR is legally required to export a practice's full
Electronic Health Information (21st Century Cures Act, §170.315(b)(10)) — but
the law doesn't say in what *format*. So practices that exercise their right
get a pile of raw vendor tables, and the things that matter most — the
clinical notes — routinely fail to survive migration to a new system. 73% of
healthcare organizations hit significant complications during EHR migrations;
legacy-archive vendors charge $5,000–$150,000; small practices get priced out
or locked in.

Anastomosis is the missing last mile, free and open source:

1. **Ingest** raw EHI exports (Practice Fusion/Tebra TSV, C-CDA/CCD, more
   adapters coming) into a lossless canonical model — every unmapped field
   preserved, nothing silently dropped.
2. **Reconstruct** human-readable, pixel-faithful clinical documents
   (template packs; the Practice Fusion SOAP-note pack replicates the original
   to forensic standards — or *learn a new layout from your own sample PDFs*).
3. **Verify** with a multi-layer QA engine (data-integrity, layout, identity
   checks) descended from a production system that reconstructed **12,906
   SOAP notes at a 100% final QA pass rate**.
4. **Deliver** by the shortest available path: vendor API where one exists,
   C-CDA import where supported, verified browser automation (with a
   six-layer wrong-patient defense) where neither does — or build a
   **searchable offline archive** that replaces paid legacy-archive
   subscriptions with plain folders, PDFs, and JSON readable for decades.

Local-first by design: **the core pipeline makes zero network calls.**
Your records never leave your machine.

## Status

Early development — see [docs/PLAN.md](docs/PLAN.md) for the living roadmap.

| Milestone | State |
|---|---|
| M0 Bootstrap (CI, PHI guardrails, CLI skeleton) | ✅ |
| M1 Archive vertical slice (ingest → reconstruct → QA → archive) | in progress |
| M2 Migration mode (verified delivery engine, destination packs, router) | planned |
| M3 Pack-from-samples layout learner | planned |
| M4 Desktop GUI (liquid-glass) | planned |

## Quickstart (will stabilize at M1)

```bash
pipx install "anastomosis[render]"     # not yet on PyPI — for now: pip install -e ".[dev]"
anast --version
anast pipeline run --source pf-tebra ./my_ehi_export \
      --pack practice_fusion_soap --deliver archive --out ./my_archive
```

## Privacy & safety

- **No PHI in this repository, ever.** All fixtures are synthetic
  (Synthea-generated or hand-built with `feedface-` GUIDs). A hashed
  deny-list scanner (`tools/phi_scan.py`) runs on every commit and in CI.
- You run this software on machines you control; you are responsible for
  HIPAA compliance in your environment. See [docs/SECURITY.md](docs/SECURITY.md)
  and [docs/DISCLAIMER.md](docs/DISCLAIMER.md).

## License

[AGPL-3.0-or-later](LICENSE). Free for everyone to use, study, and improve —
and anyone who offers it as a service must share their changes back.
No CLA: contributors keep their copyright, which makes proprietary
relicensing permanently impossible.

## Provenance

Generalized from a private production system that migrated a real clinic's
complete chart history — 12,906 reconstructed SOAP-note PDFs uploaded with
zero wrong-patient events — collapsing an estimated five months of manual
re-entry into hours. This project exists so the next practice doesn't have
to build it again.

*Built as a Harvard CS50x final project — and built to outlive it.*
