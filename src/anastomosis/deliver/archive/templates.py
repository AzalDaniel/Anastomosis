"""Jinja2 templates for the offline archive.

Three pages: :data:`INDEX_HTML` (search + inline-JSON data, the only page
with a script tag), :data:`PATIENT_HTML` and :data:`ENCOUNTER_HTML` (no
JavaScript at all). Every template: strict CSP, relative asset paths only,
static and openable from ``file://`` (RULES.md 38-39).

All user-supplied strings autoescape; the only literal HTML is the
templates' own structural markup."""

from __future__ import annotations

from jinja2 import Environment, select_autoescape

__all__ = [
    "CSP_META_CONTENT",
    "ENCOUNTER_HTML",
    "INDEX_HTML",
    "PATIENT_HTML",
    "build_env",
]

# Locked-down CSP (RULES.md 38-39). frame-ancestors is deliberately absent:
# <meta> CSP ignores it, and file:// pages have no server header to send it.
CSP_META_CONTENT = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'none'"
)


INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{{ csp | safe }}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<link rel="stylesheet" href="{{ asset_prefix }}assets/anast.css">
</head>
<body>
<header>
<h1>{{ title }}</h1>
<div class="meta">{{ patient_count }} patient(s) &middot; {{ encounter_count }} encounter(s) &middot; generated {{ generated_at }} by {{ generator }}</div>
</header>
<div class="phi-warning"><strong>PHI WARNING.</strong> This archive may contain Protected Health Information. Handle accordingly: do not upload to consumer cloud storage, do not share by email, store on encrypted media.</div>
<input id="q" type="search" placeholder="Search by name, DOB, chief complaint…" autocomplete="off">
<ul id="results" class="patient-list"></ul>
<script type="application/json" id="anast-index">{{ index_json | safe }}</script>
<script src="{{ asset_prefix }}assets/anast-index.js"></script>
<footer>Anastomosis offline archive &middot; openable from file:// with no network.</footer>
</body>
</html>
"""


PATIENT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{{ csp | safe }}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ display_name }} &mdash; chart</title>
<link rel="stylesheet" href="{{ asset_prefix }}assets/anast.css">
</head>
<body>
<header>
<h1>{{ display_name }}</h1>
<div class="meta">
{% if dob %}DOB {{ dob }} &middot; {% endif %}
{% if sex %}{{ sex }} &middot; {% endif %}
patient id <code>{{ patient_id }}</code>
</div>
</header>
<div class="phi-warning"><strong>PHI WARNING.</strong> This page contains Protected Health Information about one patient.</div>
<p><a class="button" href="bundle.json">Download FHIR Bundle (JSON)</a> <a class="button" href="{{ asset_prefix }}index.html">Back to archive</a></p>
{% if identifiers %}
<div class="section"><h3>Identifiers</h3>
<table><thead><tr><th>Kind</th><th>Value</th></tr></thead><tbody>
{% for ident in identifiers %}<tr><td>{{ ident.kind }}</td><td><code>{{ ident.value }}</code></td></tr>{% endfor %}
</tbody></table></div>
{% endif %}
<div class="section"><h3>Encounters ({{ encounters | length }})</h3>
{% if encounters %}
<ul class="encounter-list">
{% for enc in encounters %}<li><a href="encounters/{{ enc.safe_id }}.html">{{ enc.label }}</a>{% if enc.chief_complaint %} &mdash; {{ enc.chief_complaint }}{% endif %}</li>{% endfor %}
</ul>
{% else %}<p>No encounters recorded.</p>{% endif %}
</div>
{% if attachments %}
<div class="section"><h3>Documents ({{ attachments | length }})</h3>
<ul class="attachment-list">
{% for doc in attachments %}<li><a href="attachments/{{ doc.name }}">{{ doc.title }}</a>{% if doc.pages %} &mdash; {{ doc.pages }} page{% if doc.pages != 1 %}s{% endif %}{% endif %}</li>{% endfor %}
</ul>
</div>
{% endif %}
{% if conditions %}
<div class="section"><h3>Conditions ({{ conditions | length }})</h3>
<ul>{% for c in conditions %}<li>{{ c }}</li>{% endfor %}</ul></div>
{% endif %}
{% if allergies %}
<div class="section"><h3>Allergies ({{ allergies | length }})</h3>
<ul>{% for a in allergies %}<li>{{ a }}</li>{% endfor %}</ul></div>
{% endif %}
{% if medications %}
<div class="section"><h3>Medications ({{ medications | length }})</h3>
<ul>{% for m in medications %}<li>{{ m }}</li>{% endfor %}</ul></div>
{% endif %}
<footer>Generated {{ generated_at }} by {{ generator }}.</footer>
</body>
</html>
"""


ENCOUNTER_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{{ csp | safe }}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ label }} &mdash; encounter</title>
<link rel="stylesheet" href="{{ asset_prefix }}assets/anast.css">
</head>
<body>
<header>
<h1>{{ label }}</h1>
<div class="meta">
{% if date_of_service %}Date of service: {{ date_of_service }} &middot; {% endif %}
patient: <a href="../index.html">{{ display_name }}</a>
</div>
</header>
<div class="phi-warning"><strong>PHI WARNING.</strong> Encounter detail; treat as PHI.</div>
{% if chart_missing %}<div class="missing-chart"><strong>The chart for this visit is missing.</strong>
It was rendered for this patient, and the file was not there when this archive was built.
Deliver again from the folder the charts were rendered into.</div>{% endif %}
<p>
{% if pdf_name %}<a class="button" href="../pdfs/{{ pdf_name }}">Open rendered chart (PDF)</a>{% endif %}
<a class="button" href="../index.html">Patient summary</a>
<a class="button" href="{{ asset_prefix }}index.html">Back to archive</a>
</p>
{% if chief_complaint %}<div class="section"><h3>Chief complaint</h3><p>{{ chief_complaint }}</p></div>{% endif %}
{% if note_type %}<div class="section"><h3>Note type</h3><p>{{ note_type }}</p></div>{% endif %}
{% if sections %}
<div class="section"><h3>Sections</h3>
<ul>{% for s in sections %}<li><strong>{{ s.kind }}</strong>{% if s.title %} &mdash; {{ s.title }}{% endif %}{% if s.text %}: {{ s.text }}{% endif %}</li>{% endfor %}</ul>
</div>
{% endif %}
{% if addenda %}
<div class="section"><h3>Addenda ({{ addenda | length }})</h3>
<ul>{% for a in addenda %}<li>{% if a.author %}{{ a.author }}{% if a.at %} &mdash; {{ a.at }}{% endif %}: {% endif %}{{ a.text }}</li>{% endfor %}</ul>
</div>
{% endif %}
<footer>Generated {{ generated_at }} by {{ generator }}.</footer>
</body>
</html>
"""


README_TEXT = """\
Anastomosis offline archive
============================

This folder is a self-contained clinical archive: plain HTML, JSON, and PDF
files arranged so the records remain readable long after any single piece of
software ages out.

How to read it
--------------
1. Open ``index.html`` in any web browser (double-click on most systems).
2. Use the search box to find a patient by name, DOB, or chief complaint.
3. Click a patient to see their encounter list, downloadable FHIR Bundle, and
   per-encounter rendered chart PDFs.

The archive is fully offline: every link works from a ``file://`` URL with no
network connection. The HTML pages declare a Content-Security-Policy that
forbids any outbound request; the search index is plain JSON embedded in
``index.html``.

Where the data lives
--------------------
* ``patients/<patient_id>/index.html``      — patient summary (human-readable).
* ``patients/<patient_id>/bundle.json``     — FHIR R4 Bundle (machine-readable).
* ``patients/<patient_id>/encounters/*.html`` — per-encounter pages.
* ``patients/<patient_id>/pdfs/*.pdf``      — rendered chart PDFs.
* ``index.json``                            — top-level manifest of all patients.
* ``unattributed/*.pdf``                    — charts that could not be matched to
  a patient in this run, filed here rather than guessed onto someone. The folder
  exists only when there were any, and the run summary says how many. Read them
  before treating the archive as complete.

PHI warning
-----------
This archive may contain Protected Health Information. Do not upload to
consumer cloud storage, do not share by email, store on encrypted media,
and destroy securely when your retention period ends.

Licenses
--------
See ``LICENSES/`` for the licenses of bundled assets. The Anastomosis tooling
itself is AGPL-3.0-or-later (https://github.com/AzalDaniel/Anastomosis).
"""


def build_env() -> Environment:
    """Jinja2 environment for rendering the archive's HTML pages.

    Autoescape is on for every emitted template, so any string-typed value
    picked out of a record is HTML-escaped automatically."""
    return Environment(
        autoescape=select_autoescape(default=True, default_for_string=True),
        keep_trailing_newline=True,
    )
