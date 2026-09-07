"""Selector-discovery wizard support (the ``anast destination init`` engine).

PHI-free, browser-free machinery the CLI drives: :data:`SLOT_GUIDANCE`
(per-slot help); :class:`SelectorValidator`, a seam so ``--validate`` needs
no Playwright in tests (:class:`CdpSelectorValidator` is the real one);
``selectors.yaml`` rendering/writing; and :func:`registry_overlay_snippet`,
the printed (never auto-applied) registry patch.

PHI: selectors are vendor DOM, never patient data; never writes credentials.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from anastomosis.core.clock import now as _clock_now
from anastomosis.destinations.browserpack import SelectorMap

__all__ = [
    "SLOT_GUIDANCE",
    "CdpSelectorValidator",
    "SelectorValidator",
    "registry_overlay_snippet",
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
    # The upload dialog's own fields: each names the ROLE an operator looks
    # for, each skippable. ``{idx}`` stands in for the row number where a
    # dialog numbers fields per queued document.
    "upload_filename_input": "(optional) the box holding the document's display name",
    "upload_category_select": "(optional) the dropdown choosing the document's category",
    "upload_status_select": "(optional) the dropdown choosing the document's status",
    "upload_date_input": "(optional) the box holding the document's date",
    "upload_patient_prefill": "(optional) the box showing which patient the dialog will file for",
    "upload_provider_select": "(optional) the dropdown choosing the provider to file under",
    "upload_comments_input": "(optional) the box for a note attached to the document",
}

_SELECTORS_FILE = "selectors.yaml"
_WIZARD_CMD = "anast destination init"


@runtime_checkable
class SelectorValidator(Protocol):
    """The seam the ``--validate`` path checks a pasted selector through.

    ``count(selector)`` matches on the operator's CURRENT page (>=1
    accepted); a protocol so tests inject a fake with no browser.
    """

    def count(self, selector: str) -> int:
        """Return the number of elements matching ``selector`` on the current page."""
        ...


class CdpSelectorValidator:
    """A :class:`SelectorValidator` backed by a CDP-attached Playwright page.

    Constructed with a live :class:`~anastomosis.destinations.browserpack.PageLike`;
    counts matches via ``query_selector_all_text`` only, so this class needs
    no browser to import.
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def count(self, selector: str) -> int:
        return len(self._page.query_selector_all_text(selector))


def _render_selectors_yaml(
    name: str, selectors: Mapping[str, str], *, now: datetime | None = None
) -> str:
    """Render the ``selectors.yaml`` overlay text (header comment + slots).

    The header records generator, date, pack and re-run command. Slots in
    canonical order; an unset optional slot is empty (the loader skips it).
    """
    stamp = (now or _clock_now()).date().isoformat()
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
    target.write_text(_render_selectors_yaml(name, selectors, now=now), encoding="utf-8")
    return target


def registry_overlay_snippet(name: str) -> str:
    """The printed registry-overlay snippet flipping ``name`` to the browser pack.

    ``registry.yaml`` is the single routing truth and is NEVER
    auto-modified; the operator pastes this into their own ``--registry``
    overlay.
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
