"""Generic, selector-driven destination pack machinery (PLAN item 12b).

A *browser pack* teaches Anastomosis how to file a reconstructed chart into one
foreign EHR through its web UI — the route taken when no vendor API and no
C-CDA import exist (the common case for the practices this tool serves). The
upload engine (:mod:`anastomosis.deliver.browser.engine`) never touches a
browser directly: it speaks only to the :mod:`anastomosis.destinations.base`
protocols. This module implements those protocols *generically*, driven by a
table of CSS selectors and a small config — so a concrete pack
(``destinations/tebra``) is data (a ``pack.yaml`` of selector slots), never code.

The seam to the browser is deliberately thin. :class:`BrowserPackDestination`
drives a :class:`PageLike` — the minimal page interface this module needs and
nothing more — so the whole pack is testable against an in-memory fake page
with no Playwright anywhere. A real Playwright ``Page`` does not match
``PageLike`` directly; :class:`PlaywrightPageAdapter` wraps one (with a lazy
import, like :func:`anastomosis.deliver.browser.cdp.connect_over_cdp`).

Two safety properties are baked into the shapes here:

* **No selector is invented.** A pack ships every selector slot marked
  ``DISCOVER`` until an operator fills it via ``anast destination init``;
  :meth:`SelectorMap.from_yaml_dict` raises :class:`PackNotReadyError` naming the
  undiscovered slots, so a half-discovered pack refuses to run rather than
  guessing the destination's DOM.
* **Ambiguity is never guessed past.** The resolver matches a patient by an
  EXACT rendered name AND DOB; zero matches return ``None`` (not found, never a
  best guess) and MULTIPLE exact matches raise
  :class:`~anastomosis.deliver.browser.errors.PermanentDeliveryError` — filing
  against a guessed row is the wrong-patient failure this subsystem exists to
  prevent.

PHI rule (load-bearing): this module NEVER logs search text, banner text, or
row text. It logs slot *names*, counts, and ``exc_tag`` type names only — the
search term is a patient name, the banner and rows carry names and DOBs.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from anastomosis.core.logutil import exc_tag
from anastomosis.deliver.browser.errors import (
    DeliveryError,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from anastomosis.destinations.base import (
    DestinationPatient,
    UploadItem,
    UploadReceipt,
)

if TYPE_CHECKING:
    from anastomosis.core.model import Patient

__all__ = [
    "BrowserPackConfig",
    "BrowserPackDestination",
    "PackNotReadyError",
    "PageLike",
    "PlaywrightPageAdapter",
    "SelectorMap",
]

logger = logging.getLogger(__name__)

# The literal prefix a shipped (undiscovered) selector slot carries. A pack
# loaded with any slot still starting with this refuses to run — the operator
# must run the discovery wizard first. Kept as a prefix (not the whole string)
# so the shipped scaffold can append a human instruction after it.
DISCOVER_PREFIX = "DISCOVER"

# The wizard command that fills the undiscovered slots — named in the
# PackNotReadyError so the operator knows exactly what to run.
_WIZARD_HINT = "anast destination init"

# Required selector slots: a pack missing any of these (or leaving any at the
# DISCOVER placeholder) cannot run. Ordered for the wizard's required-first prompt.
_REQUIRED_SLOTS: tuple[str, ...] = (
    "patient_search_input",
    "patient_search_submit",
    "patient_result_row",
    "patient_banner_name",
    "patient_banner_dob",
    "documents_list_item",
    "upload_file_input",
    "upload_submit",
    "upload_success_marker",
)

# Optional selector slots: a pack may leave these unset (empty string) — the
# driver clicks them only when configured.
_OPTIONAL_SLOTS: tuple[str, ...] = (
    "documents_tab",
    "upload_open_button",
)


@runtime_checkable
class PageLike(Protocol):
    """The thin browser-page seam a browser pack drives — and nothing more.

    A real Playwright ``Page`` does not satisfy this directly (its signatures
    differ); :class:`PlaywrightPageAdapter` wraps one. Keeping the seam this
    small is what lets the whole pack be exercised against an in-memory fake
    page with no Playwright in the test environment.
    """

    def goto(self, url: str) -> None:
        """Navigate the page to ``url``."""
        ...

    def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the element matched by ``selector``."""
        ...

    def click(self, selector: str) -> None:
        """Click the element matched by ``selector``."""
        ...

    def text_content(self, selector: str) -> str | None:
        """Return the text of the first element matched by ``selector``."""
        ...

    def query_selector_all_text(self, selector: str) -> list[str]:
        """Return the text of EVERY element matched by ``selector``."""
        ...

    def set_input_files(self, selector: str, path: str) -> None:
        """Set the file-input matched by ``selector`` to the file at ``path``."""
        ...

    def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        """Wait up to ``timeout_ms`` for ``selector`` to appear; raise on timeout."""
        ...

    def is_closed(self) -> bool:
        """Whether the page (and its browser) has been closed."""
        ...


class PlaywrightPageAdapter:
    """Wrap a real Playwright ``Page`` into :class:`PageLike`.

    The Playwright import is lazy (the adapter is constructed with an already
    live ``Page``), so this module loads on a machine without the
    ``deliver-browser`` extra — the same discipline as
    :func:`anastomosis.deliver.browser.cdp.connect_over_cdp`. The Playwright
    methods named here differ from ours (``query_selector_all`` returns element
    handles; ``text_content`` lives on the handle), so the adapter is the only
    place those signatures are bridged.
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def goto(self, url: str) -> None:  # pragma: no cover - needs playwright
        self._page.goto(url)

    def fill(self, selector: str, value: str) -> None:  # pragma: no cover - needs playwright
        self._page.fill(selector, value)

    def click(self, selector: str) -> None:  # pragma: no cover - needs playwright
        self._page.click(selector)

    def text_content(self, selector: str) -> str | None:  # pragma: no cover - needs playwright
        result = self._page.text_content(selector)
        return None if result is None else str(result)

    def query_selector_all_text(
        self, selector: str
    ) -> list[str]:  # pragma: no cover - needs playwright
        handles = self._page.query_selector_all(selector)
        return [h.text_content() or "" for h in handles]

    def set_input_files(
        self, selector: str, path: str
    ) -> None:  # pragma: no cover - needs playwright
        self._page.set_input_files(selector, path)

    def wait_for_selector(
        self, selector: str, timeout_ms: int
    ) -> None:  # pragma: no cover - needs playwright
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    def is_closed(self) -> bool:  # pragma: no cover - needs playwright
        return bool(self._page.is_closed())


class PackNotReadyError(Exception):
    """A pack still carries undiscovered selector slots — it refuses to run.

    Raised by :meth:`SelectorMap.from_yaml_dict` when any required slot is left
    at the ``DISCOVER`` placeholder the shipped scaffold ships with. The message
    names every undiscovered slot and the wizard command that fills them, so the
    failure is actionable rather than a mysterious crash mid-run.
    """

    def __init__(self, pack_name: str, undiscovered: tuple[str, ...]) -> None:
        self.pack_name = pack_name
        self.undiscovered = undiscovered
        slots = ", ".join(undiscovered)
        super().__init__(
            f"destination pack {pack_name!r} is not ready: {len(undiscovered)} selector "
            f"slot(s) still undiscovered ({slots}). Run: {_WIZARD_HINT} {pack_name}"
        )


@dataclass(frozen=True)
class SelectorMap:
    """The CSS selectors a browser pack drives, one per UI slot.

    Required slots must all be present and non-empty (a missing required slot is
    a malformed pack and raises ``KeyError``); optional slots default to the
    empty string (the driver acts on them only when set). A slot left at the
    ``DISCOVER`` placeholder makes :meth:`from_yaml_dict` raise
    :class:`PackNotReadyError` — the shipped scaffold cannot run until the
    discovery wizard fills it.
    """

    # required
    patient_search_input: str
    patient_search_submit: str
    patient_result_row: str
    patient_banner_name: str
    patient_banner_dob: str
    documents_list_item: str
    upload_file_input: str
    upload_submit: str
    upload_success_marker: str
    # optional
    documents_tab: str = ""
    upload_open_button: str = ""

    @classmethod
    def required_slots(cls) -> tuple[str, ...]:
        return _REQUIRED_SLOTS

    @classmethod
    def optional_slots(cls) -> tuple[str, ...]:
        return _OPTIONAL_SLOTS

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any], *, pack_name: str) -> SelectorMap:
        """Build a validated :class:`SelectorMap` from a pack's ``selectors:`` block.

        Validation order (loud, never silent):

        1. Every required slot must be present and a non-empty string — a
           missing/blank required slot is a malformed pack (``KeyError`` /
           ``ValueError`` naming the slot).
        2. Any slot whose value still starts with ``DISCOVER`` is undiscovered;
           if any required slot is undiscovered, raise :class:`PackNotReadyError`
           listing them all and the wizard command (an undiscovered OPTIONAL
           slot is treated as "skip" — left empty — not a blocker).
        """
        missing = [s for s in _REQUIRED_SLOTS if s not in data]
        if missing:
            raise KeyError(
                f"destination pack {pack_name!r} selectors missing required slot(s): "
                f"{', '.join(missing)}"
            )

        values: dict[str, str] = {}
        undiscovered: list[str] = []
        for slot in (*_REQUIRED_SLOTS, *_OPTIONAL_SLOTS):
            raw = data.get(slot, "")
            if not isinstance(raw, str):
                raise ValueError(
                    f"destination pack {pack_name!r} selector {slot!r} must be a string"
                )
            value = raw.strip()
            if slot in _REQUIRED_SLOTS and not value:
                raise ValueError(
                    f"destination pack {pack_name!r} required selector {slot!r} is empty"
                )
            if value.startswith(DISCOVER_PREFIX):
                if slot in _REQUIRED_SLOTS:
                    undiscovered.append(slot)
                # An optional slot left at DISCOVER is simply "not discovered
                # yet" — treat it as skipped (empty) rather than a blocker.
                value = ""
            values[slot] = value

        if undiscovered:
            raise PackNotReadyError(pack_name, tuple(undiscovered))
        return cls(**values)


@dataclass(frozen=True)
class BrowserPackConfig:
    """The non-selector knobs of a browser pack.

    ``patient_search_url`` is the page the resolver navigates to before
    searching; ``None`` means the operator navigates to the patient list
    themselves before the run (some EHRs have no stable deep link). ``dob_format``
    is a ``%m/%d/%Y``-style template rendered from the integer date parts (NEVER
    platform ``strftime`` — ``%-d``/``%-m`` are glibc-only and this runs on
    Windows CI too). ``search_by`` selects which fields are typed into the search
    box; ``result_match`` is fixed to ``exact_name_dob`` in v1 — the SAFE mode
    that never guesses past an ambiguous result.
    """

    name: str
    patient_search_url: str | None = None
    dob_format: str = "%m/%d/%Y"
    search_by: Literal["name", "dob", "both"] = "both"
    result_match: Literal["exact_name_dob"] = "exact_name_dob"
    success_timeout_ms: int = 30000

    def render_dob(self, value: date) -> str:
        """Render ``value`` using ``dob_format`` from the integer date parts.

        Supports the common ``strftime`` directives a DOB needs —
        ``%m``/``%d``/``%Y``/``%y`` (zero-padded) and ``%-m``/``%-d`` (unpadded)
        — built BY HAND from ``value.month``/``.day``/``.year`` so the result is
        identical on every platform (the ``date_renderings`` lesson in
        :mod:`anastomosis.deliver.verify.levels`). A literal ``%%`` is an escaped
        percent; any other ``%X`` is passed through unchanged.
        """
        out: list[str] = []
        i = 0
        fmt = self.dob_format
        while i < len(fmt):
            ch = fmt[i]
            if ch != "%":
                out.append(ch)
                i += 1
                continue
            # A trailing bare '%' is passed through literally.
            token = fmt[i : i + 3] if fmt[i + 1 : i + 2] == "-" else fmt[i : i + 2]
            out.append(self._dob_token(token, value))
            i += len(token)
        return "".join(out)

    @staticmethod
    def _dob_token(token: str, value: date) -> str:
        return {
            "%m": f"{value.month:02d}",
            "%-m": str(value.month),
            "%d": f"{value.day:02d}",
            "%-d": str(value.day),
            "%Y": str(value.year),
            "%y": f"{value.year % 100:02d}",
            "%%": "%",
        }.get(token, token)


class BrowserPackDestination:
    """The aggregate :class:`~anastomosis.destinations.base.Destination`, generic.

    Built from a :class:`SelectorMap`, a :class:`PageLike`, and a
    :class:`BrowserPackConfig`; implements every role protocol the engine drives
    (session/resolver/banner/scanner/driver) by reading and acting on selectors.
    One instance is both the destination and each of its collaborators — the
    same single-object pattern :class:`anastomosis.deliver.browser.fake.FakeDestination`
    uses — so the engine holds one object.
    """

    def __init__(self, selectors: SelectorMap, page: PageLike, config: BrowserPackConfig) -> None:
        self._selectors = selectors
        self._page = page
        self._config = config

    # --- Destination protocol ---

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def session(self) -> BrowserPackDestination:
        return self

    @property
    def resolver(self) -> BrowserPackDestination:
        return self

    @property
    def banner(self) -> BrowserPackDestination:
        return self

    @property
    def scanner(self) -> BrowserPackDestination:
        return self

    @property
    def driver(self) -> BrowserPackDestination:
        return self

    # --- Session ---
    #
    # In CDP mode Anastomosis attaches to a browser the OPERATOR launched and
    # logged into; we never own its lifecycle. open() is therefore a no-op (the
    # operator already established the session) and close() NEVER closes the
    # page — closing the operator's browser out from under them would end a
    # session we do not own. Liveness is simply "the page is not closed".

    def open(self) -> None:
        return None

    def close(self) -> None:
        # Deliberately a no-op: we never own the operator's browser in CDP mode.
        return None

    def is_alive(self) -> bool:
        return not self._page.is_closed()

    # --- PatientResolver ---

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        """Search the destination and return the EXACTLY-matched patient row.

        Navigates to the search URL (when configured), types the search terms,
        submits, and reads back every result row's rendered text. A row is a
        match when it contains BOTH the patient's rendered name parts AND the
        rendered DOB (``result_match=exact_name_dob`` — the only v1 mode):

        * zero matches -> ``None`` (not found; never a best guess);
        * exactly one match -> click that row (open the chart for the banner
          readback) and return a :class:`DestinationPatient` whose id is a hash
          of the matched row text (a row index is not stable across renders),
          with ``matched_on=("name", "dob")``;
        * multiple exact matches -> :class:`PermanentDeliveryError`. Ambiguity is
          NEVER guessed past: two patients matching the same name AND DOB is a
          condition only a human can safely resolve, so the item fails
          permanently rather than risk filing into the wrong chart.

        PHI: logs slot names and the match COUNT only — never the search text or
        any row text.
        """
        if self._config.patient_search_url is not None:
            self._page.goto(self._config.patient_search_url)

        self._fill_search(patient)
        self._page.click(self._selectors.patient_search_submit)

        rows = self._page.query_selector_all_text(self._selectors.patient_result_row)
        matches = [i for i, row in enumerate(rows) if self._row_matches(row, patient)]
        logger.info(
            "resolver: %d row(s), %d exact match(es) on (name, dob)", len(rows), len(matches)
        )
        if not matches:
            return None
        if len(matches) > 1:
            # Ambiguity is never guessed past — a human must disambiguate.
            raise PermanentDeliveryError(
                "multiple destination rows match the same name and DOB; "
                "ambiguous patient is never auto-selected"
            )

        index = matches[0]
        # Open the chart so the banner readback can confirm the patient.
        self._page.click(self._selectors.patient_result_row)
        # A row index is not a stable identity (re-rendered lists reorder), so
        # the id is a hash of the matched row's full text — stable for the same
        # rendered row, and PHI-safe because it is a one-way digest, never the
        # text itself.
        row_id = hashlib.sha256(rows[index].encode("utf-8")).hexdigest()[:16]
        return DestinationPatient(
            destination_patient_id=f"row:{row_id}", matched_on=("name", "dob")
        )

    def _fill_search(self, patient: Patient) -> None:
        """Type the configured search terms into the search input.

        ``search_by`` chooses what is typed: the name (family + given), the
        rendered DOB, or both joined by a space. The single search input takes
        the combined query — packs whose UI splits name and DOB into two boxes
        are a v2 shape (documented in the wizard guidance). PHI: never logged.
        """
        terms = self._search_terms(patient)
        self._page.fill(self._selectors.patient_search_input, terms)

    def _search_terms(self, patient: Patient) -> str:
        name = self._name_query(patient)
        dob = self._render_dob(patient)
        if self._config.search_by == "name":
            return name
        if self._config.search_by == "dob":
            return dob
        return " ".join(p for p in (name, dob) if p)  # "both"

    # --- BannerCheck ---

    def current_patient_matches(self, expected: Patient) -> bool:
        """Read the open chart's banner and confirm it is ``expected``.

        Reads both the banner name and DOB slots; BOTH must carry the expected
        patient's rendered name parts AND DOB rendering. Any miss returns
        ``False`` — the engine turns that into a
        :class:`~anastomosis.deliver.browser.errors.WrongPatientError` and aborts
        the whole run. PHI: logs the boolean outcome and slot names only.
        """
        banner_name = self._page.text_content(self._selectors.patient_banner_name) or ""
        banner_dob = self._page.text_content(self._selectors.patient_banner_dob) or ""
        name_ok = self._name_present(banner_name, expected)
        dob_ok = self._dob_present(banner_dob, expected)
        matches = name_ok and dob_ok
        logger.info(
            "banner check: name_ok=%s dob_ok=%s (slots patient_banner_name/dob)",
            name_ok,
            dob_ok,
        )
        return matches

    # --- ExistingDocsScanner ---

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        """Return the document titles/filenames the destination already shows.

        The destination-comparable fingerprint of an existing chart document is
        the title/filename as the destination renders it (the
        :attr:`UploadItem.fingerprint` default is the file name). PHI: logs the
        count only — a document title can embed a patient name.
        """
        texts = self._page.query_selector_all_text(self._selectors.documents_list_item)
        prints = {t.strip() for t in texts if t.strip()}
        logger.info("scanner: %d existing document fingerprint(s)", len(prints))
        return prints

    # --- UploadDriver ---

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """File ``item`` into the open chart through the upload UI.

        Step order (the wizard's discovery order, and what the e2e test pins):
        optional ``documents_tab`` click, optional ``upload_open_button`` click,
        set the file input, click submit, wait for the success marker. A timeout
        waiting for the success marker is a
        :class:`~anastomosis.deliver.browser.errors.TransientDeliveryError`
        (retryable — a slow page, not a permanent failure).

        Returns ``UploadReceipt(destination_doc_id=None, echoed_size_bytes=None)``:
        browser uploads rarely echo a doc id or size, and L6 read-back is the
        verifier's job — the receipt does not pretend to information the UI did
        not give. PHI: logs the item key and slot names only.
        """
        if self._selectors.documents_tab:
            self._page.click(self._selectors.documents_tab)
        if self._selectors.upload_open_button:
            self._page.click(self._selectors.upload_open_button)
        self._page.set_input_files(self._selectors.upload_file_input, str(item.file_path))
        self._page.click(self._selectors.upload_submit)
        try:
            self._page.wait_for_selector(
                self._selectors.upload_success_marker, self._config.success_timeout_ms
            )
        except DeliveryError:
            # A DeliveryError raised through the page seam already carries the
            # engine's routing semantics — never downgrade it to transient.
            raise
        except Exception as exc:
            # A missing success marker within the timeout is retryable: the page
            # may simply be slow. Re-raise as the engine's transient signal,
            # logging the item key + exc TYPE only (never the page text).
            logger.warning(
                "upload success marker not seen for item %s (%s)", item.item_key, exc_tag(exc)
            )
            raise TransientDeliveryError(
                "upload success marker not observed within timeout"
            ) from exc
        logger.info("upload filed item %s (slot upload_success_marker seen)", item.item_key)
        # Browser uploads rarely echo a doc id or size — L6 read-back verifies.
        return UploadReceipt(destination_doc_id=None, echoed_size_bytes=None)

    # --- matching helpers (PHI-safe: never log the values they compare) ---

    def _render_dob(self, patient: Patient) -> str:
        return self._config.render_dob(patient.birth_date) if patient.birth_date else ""

    @staticmethod
    def _name_query(patient: Patient) -> str:
        """The name terms typed into the search box: family then given."""
        parts = [patient.family_name, patient.given_name]
        return " ".join(p for p in parts if p)

    @staticmethod
    def _name_parts(patient: Patient) -> list[str]:
        return [p for p in (patient.family_name, patient.given_name) if p]

    def _name_present(self, text: str, patient: Patient) -> bool:
        """Whether every (case-insensitive) name part appears in ``text``."""
        parts = self._name_parts(patient)
        if not parts:
            return False
        hay = text.lower()
        return all(p.lower() in hay for p in parts)

    def _dob_present(self, text: str, patient: Patient) -> bool:
        """Whether the rendered DOB appears in ``text`` (always required)."""
        dob = self._render_dob(patient)
        if not dob:
            # No DOB to match means the exact-name-dob contract cannot be met:
            # fail closed rather than match on name alone (a name collision is
            # exactly what the DOB gate defends against).
            return False
        return dob in text

    def _row_matches(self, row_text: str, patient: Patient) -> bool:
        """A result row matches when BOTH name parts AND DOB are present in it."""
        return self._name_present(row_text, patient) and self._dob_present(row_text, patient)
