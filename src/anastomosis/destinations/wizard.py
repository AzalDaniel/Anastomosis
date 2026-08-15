"""Selector-discovery wizard support (the ``anast destination init`` engine).

Discovering a browser pack means an operator pasting the CSS selector for each
UI slot, derived from THEIR logged-in EHR session — Anastomosis ships none (the
no-hallucination rule). This module holds the PHI-free, browser-free machinery
the CLI command drives:

* :data:`SLOT_GUIDANCE` — one generic line of help per selector slot.
* :class:`SelectorValidator` — the SEAM the optional ``--validate`` path uses to
  check a pasted selector against the operator's current page (>=1 match). It is
  a small protocol so tests inject a fake validator and CI never needs
  Playwright; the real CDP-backed validator (:class:`CdpSelectorValidator`)
  imports Playwright lazily, like
  :func:`anastomosis.deliver.browser.cdp.connect_over_cdp`.
* :func:`render_selectors_yaml` — the ``selectors.yaml`` text (header comment +
  slots) written into the user directory; and :func:`write_selectors` which
  creates the ``0o700`` directory and writes the file.
* :func:`registry_overlay_snippet` — the printed (never auto-applied) registry
  overlay flipping the destination's ``browser`` capability to the pack.

PHI rule: a CSS selector is vendor DOM, never patient data; this module carries
selector strings, slot names, and dates only — nothing patient-derived, and it
never writes credentials.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from anastomosis.destinations.browserpack import SelectorMap

__all__ = [
    "SLOT_GUIDANCE",
    "CdpSelectorValidator",
    "SelectorValidator",
    "registry_overlay_snippet",
    "render_selectors_yaml",
    "write_selectors",
]

# One generic line of guidance per slot. Deliberately vendor-neutral: it
# describes the ROLE of the element ("the search box where you type a patient's
# name"), never a Tebra-specific label — the operator maps it to their own UI.
SLOT_GUIDANCE: Mapping[str, str] = {
    "patient_search_input": "the search box where you type a patient's name",
    "patient_search_submit": "the button that runs the patient search",
    "patient_result_row": "one row in the patient-search results list",
    "patient_banner_name": "the patient name shown in the open chart's banner",
    "patient_banner_dob": "the date of birth shown in the open chart's banner",
    "documents_list_item": "one entry in the chart's existing-documents list",
    "upload_file_input": "the file <input> the chart upload form uses",
    "upload_submit": "the button that submits the document upload",
    "upload_success_marker": "an element that appears only after a successful upload",
    "documents_tab": "(optional) the tab/link that opens the Documents area",
    "upload_open_button": "(optional) the button that opens the upload dialog",
}

_SELECTORS_FILE = "selectors.yaml"
_WIZARD_CMD = "anast destination init"


@runtime_checkable
class SelectorValidator(Protocol):
    """The seam the ``--validate`` path checks a pasted selector through.

    ``count(selector)`` returns how many elements on the operator's CURRENT page
    match — the wizard accepts a selector matching >=1. A protocol (not a
    concrete class) so tests inject a fake and the validation path needs no
    browser; the real implementation is :class:`CdpSelectorValidator`.
    """

    def count(self, selector: str) -> int:
        """Return the number of elements matching ``selector`` on the current page."""
        ...


class CdpSelectorValidator:
    """A :class:`SelectorValidator` backed by a CDP-attached Playwright page.

    Constructed with a live :class:`anastomosis.destinations.browserpack.PageLike`
    (the CLI attaches over CDP and wraps the page in a
    :class:`~anastomosis.destinations.browserpack.PlaywrightPageAdapter`). Kept
    thin and Playwright-free at this layer — it only counts matches via the
    page's ``query_selector_all_text`` seam — so it needs no browser to import.
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def count(self, selector: str) -> int:
        return len(self._page.query_selector_all_text(selector))


def render_selectors_yaml(
    name: str, selectors: Mapping[str, str], *, now: datetime | None = None
) -> str:
    """Render the ``selectors.yaml`` overlay text (header comment + slots).

    The header records that the file is generated, by what, when, for which pack,
    and how to re-run discovery — so a file found later explains itself. Slots
    are written in canonical order (required then optional); an unset optional
    slot is written as an empty string (the loader treats empty as "skip").
    ``now`` is injectable so tests get a deterministic header date.
    """
    stamp = (now or datetime.now(UTC)).date().isoformat()
    lines = [
        f"# Anastomosis destination selectors for {name!r} — GENERATED, do not hand-edit.",
        f"# generated-by: {_WIZARD_CMD} {name}",
        f"# generated-on: {stamp}",
        f"# pack: {name}",
        "#",
        "# These CSS selectors were discovered against an operator's live EHR session.",
        f"# Re-run discovery when the vendor UI changes:  {_WIZARD_CMD} {name}",
        "# This file overlays the built-in pack scaffold; the scaffold stays pristine.",
        "selectors:",
    ]
    for slot in (*SelectorMap.required_slots(), *SelectorMap.optional_slots()):
        value = selectors.get(slot, "")
        # Quote the value so any CSS metacharacters survive YAML round-trip.
        lines.append(f'  {slot}: "{_yaml_escape(value)}"')
    return "\n".join(lines) + "\n"


def _yaml_escape(value: str) -> str:
    """Escape a string for a double-quoted YAML scalar (backslash + quote)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_selectors(
    name: str,
    selectors: Mapping[str, str],
    out_dir: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Write the ``selectors.yaml`` overlay under ``out_dir/<name>/``.

    Creates the per-pack directory ``0o700`` on POSIX (selectors are config, but
    the directory sits beside other Anastomosis state and stays owner-only by
    house policy). NEVER writes credentials. Returns the written file path.
    """
    pack_dir = out_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        pack_dir.chmod(stat.S_IRWXU)  # 0o700 — owner only
    target = pack_dir / _SELECTORS_FILE
    target.write_text(render_selectors_yaml(name, selectors, now=now), encoding="utf-8")
    return target


def registry_overlay_snippet(name: str) -> str:
    """The printed registry-overlay snippet flipping ``name`` to the browser pack.

    The packaged ``registry.yaml`` is the single routing truth and is NEVER
    auto-modified; instead the wizard PRINTS this so the operator can paste it
    into THEIR own ``--registry`` overlay file, declaring the now-discovered pack
    as a viable browser route. ``detail`` carries the pack reference the router
    surfaces in its transit map.
    """
    return (
        "entries:\n"
        f"  {name}:\n"
        f"    name: {name}\n"
        f"    display: {name}\n"
        "    doc_write_api: {kind: unverified}\n"
        "    ccda_import: {kind: unverified}\n"
        "    browser:\n"
        "      kind: pack\n"
        f"      detail: destinations/{name}\n"
    )
