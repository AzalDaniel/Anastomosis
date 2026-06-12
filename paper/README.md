# JOSS paper draft

`paper.md` + `paper.bib` follow the current (post-January-2026) JOSS format:
750–1750 words with required sections Summary, Statement of need, State of
the field, Software design, Research impact statement, AI usage disclosure,
Acknowledgements, References. Every bibliography entry was verified against
the publisher page or PubMed before inclusion.

## Build a preview PDF

JOSS compiles papers with the `openjournals/inara` Docker image (draft mode
is the default and adds a watermark + line numbers):

```bash
docker run --rm \
    --volume "$PWD/paper":/data \
    --user "$(id -u):$(id -g)" \
    --env JOURNAL=joss \
    openjournals/inara
```

## Before submitting (the author's checklist — none of this is automated)

1. **Eligibility window.** JOSS requires roughly six months of public
   repository history with sustained development, releases, and public
   issues/PRs before submission. This repo went public June 2026, so the
   realistic submission window opens around **December 2026**.
2. Fill the `TODO(author)` items in `paper.md` frontmatter: ORCID,
   affiliation country (or institution), and the actual submission date.
3. Tag a release and be ready to archive it on Zenodo at acceptance (JOSS
   requires a DOI-minted archive then, not at submission).
4. Re-verify the word count is within 750–1750 (`make` no; just:
   `pandoc --strip-comments -t plain paper.md | wc -w` minus the reference
   list, or trust the inara draft PDF's length).
5. Review the **AI usage disclosure** section personally — it is mandatory
   under JOSS's 2026 policy and must accurately describe how this project
   uses AI assistance.
6. Submit at <https://joss.theoj.org> (the submission is the author's act;
   nothing in this repo submits anything).
