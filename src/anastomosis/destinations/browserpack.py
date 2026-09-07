"""Generic, selector-driven destination pack machinery.

A browser pack files a chart into a foreign EHR through its web UI, driven
by CSS selectors and config (RULES.md 12-13, 27) — a concrete pack is data
(``destinations/tebra``), never code, driving :class:`PageLike` so the whole
pack is testable with no Playwright.

PHI: no search/banner/row/dialog text is logged, only names/counts/booleans.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from anastomosis.core.identity import date_token_present, name_parts_present
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.deliver.browser.errors import (
    DeliveryError,
    PermanentDeliveryError,
    TransientDeliveryError,
    WrongPatientError,
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

# Optional selector slots: a pack may leave these unset — the driver acts on
# them only when configured. The upload-form slots exist because attaching a
# file alone leaves a chart uncategorised, undated and under nobody. Optional
# is load-bearing: a pack leaving them blank drives only the five original
# actions, so a selectors.yaml discovered before these slots existed still loads.
_OPTIONAL_SLOTS: tuple[str, ...] = (
    "documents_tab",
    "upload_open_button",
    "upload_filename_input",
    "upload_category_select",
    "upload_status_select",
    "upload_date_input",
    "upload_patient_prefill",
    "upload_provider_select",
    "upload_comments_input",
)

# The subset of optional slots that live inside the upload form. They are the
# ones the row-index token below is rendered into, and the ones the driver
# fills/reads rather than clicks.
_FORM_SLOTS: frozenset[str] = frozenset(
    {
        "upload_filename_input",
        "upload_category_select",
        "upload_status_select",
        "upload_date_input",
        "upload_patient_prefill",
        "upload_provider_select",
        "upload_comments_input",
    }
)

# The row-index token a form slot may carry: a multi-document dialog numbers
# controls per row (``#fileNameInput0``, ``...1``), so a pack writes the
# row-agnostic ``#fileNameInput{idx}``. This engine files ONE document per
# dialog, so the rendered row is always :data:`_SINGLE_ROW_INDEX`.
_ROW_INDEX_TOKEN = "{idx}"  # noqa: S105 — a selector placeholder, not a secret
_SINGLE_ROW_INDEX = 0


@runtime_checkable
class PageLike(Protocol):
    """The thin browser-page seam a browser pack drives — and nothing more.

    A real Playwright ``Page`` does not satisfy this directly;
    :class:`PlaywrightPageAdapter` wraps one, so the pack tests against an
    in-memory fake with no Playwright.
    """

    def goto(self, url: str) -> None:
        """Navigate the page to ``url``."""
        ...

    def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the element matched by ``selector``."""
        ...

    def click(self, selector: str, *, nth: int | None = None) -> None:
        """Click the element matched by ``selector``; with ``nth``, the
        ``nth`` (0-based) match rather than the first — the resolver uses it
        to open the row it actually matched, not row 0.
        """
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

    def select_option(self, selector: str, value: str, *, by_label: bool = False) -> None:
        """Choose ``value`` in the ``<select>`` matched by ``selector``.

        ``by_label`` matches the visible LABEL, not the value attribute — a
        vendor's option values are often opaque ids. Raises when no option
        matches: a dropdown refusing the configured choice is a structural
        mismatch, never left at its default.
        """
        ...

    def input_value(self, selector: str) -> str:
        """Return the CURRENT value of the form control matched by ``selector``.

        Distinct from :meth:`text_content` on purpose: a control's value lives
        in a DOM property, not its text. This is the readback verb.
        """
        ...

    def wait_for_selector(self, selector: str, timeout_ms: int) -> None:
        """Wait up to ``timeout_ms`` for ``selector`` to appear; raise on timeout."""
        ...

    def is_closed(self) -> bool:
        """Whether the page (and its browser) has been closed."""
        ...


class PlaywrightPageAdapter:
    """Wrap a real Playwright ``Page`` into :class:`PageLike`.

    Constructed with an already-live ``Page``, so this module loads without
    the ``deliver-browser`` extra. Bridges the signature differences
    (``query_selector_all`` returns handles; ``text_content`` lives on one).
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def goto(self, url: str) -> None:  # pragma: no cover - needs playwright
        self._page.goto(url)

    def fill(self, selector: str, value: str) -> None:  # pragma: no cover - needs playwright
        self._page.fill(selector, value)

    def click(
        self, selector: str, *, nth: int | None = None
    ) -> None:  # pragma: no cover - needs playwright
        if nth is None:
            self._page.click(selector)
        else:
            # Click the specific matched row: Playwright's bare page.click()
            # targets the FIRST match, so a non-zero matched index needs the
            # locator's nth() to open the right chart.
            self._page.locator(selector).nth(nth).click()

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

    def select_option(
        self, selector: str, value: str, *, by_label: bool = False
    ) -> None:  # pragma: no cover - needs playwright
        # Playwright takes label= and value= as different keyword arguments and
        # raises when neither matches an option — which is exactly the signal
        # the driver turns into a permanent failure, so it is not swallowed here.
        if by_label:
            self._page.select_option(selector, label=value)
        else:
            self._page.select_option(selector, value=value)

    def input_value(self, selector: str) -> str:  # pragma: no cover - needs playwright
        return str(self._page.input_value(selector) or "")

    def wait_for_selector(
        self, selector: str, timeout_ms: int
    ) -> None:  # pragma: no cover - needs playwright
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    def is_closed(self) -> bool:  # pragma: no cover - needs playwright
        return bool(self._page.is_closed())


class PackNotReadyError(Exception):
    """A pack still carries undiscovered selector slots — it refuses to run.

    Raised by :meth:`SelectorMap.from_yaml_dict`; the message names every
    undiscovered slot and the wizard command that fills them.
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

    A missing required slot raises ``KeyError``; optional slots default to
    empty (acted on only when set). A slot left at ``DISCOVER`` makes
    :meth:`from_yaml_dict` raise :class:`PackNotReadyError`.
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
    # optional — navigation
    documents_tab: str = ""
    upload_open_button: str = ""
    # optional — the upload form's own fields. A form slot may carry the
    # ``{idx}`` row-index token; the driver renders it for the row it fills.
    upload_filename_input: str = ""
    upload_category_select: str = ""
    upload_status_select: str = ""
    upload_date_input: str = ""
    upload_patient_prefill: str = ""
    upload_provider_select: str = ""
    upload_comments_input: str = ""

    @classmethod
    def required_slots(cls) -> tuple[str, ...]:
        return _REQUIRED_SLOTS

    @classmethod
    def optional_slots(cls) -> tuple[str, ...]:
        return _OPTIONAL_SLOTS

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any], *, pack_name: str) -> SelectorMap:
        """Build a validated :class:`SelectorMap` from a pack's ``selectors:`` block.

        Contract: raises (never silent) if a required slot is missing/blank,
        if any declared slot name is unknown to this loader (a typo must
        never read as "ready" while going unhonoured), or lists undiscovered
        required slots via :class:`PackNotReadyError`. An undiscovered
        OPTIONAL slot is treated as skipped, not a blocker.
        """
        missing = [s for s in _REQUIRED_SLOTS if s not in data]
        if missing:
            raise KeyError(
                f"destination pack {pack_name!r} selectors missing required slot(s): "
                f"{', '.join(missing)}"
            )

        unknown = sorted(set(data) - set(_REQUIRED_SLOTS) - set(_OPTIONAL_SLOTS))
        if unknown:
            raise ValueError(
                f"destination pack {pack_name!r} declares unknown selector slot(s): "
                f"{', '.join(unknown)}"
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
            if _ROW_INDEX_TOKEN in value and slot not in _FORM_SLOTS:
                # Only the form slots are rendered against a row, so the token
                # anywhere else would reach the page verbatim and match nothing.
                raise ValueError(
                    f"destination pack {pack_name!r} selector {slot!r} carries the "
                    f"{_ROW_INDEX_TOKEN} row token, which only the upload form's "
                    f"slots render"
                )
            values[slot] = value

        if undiscovered:
            raise PackNotReadyError(pack_name, tuple(undiscovered))
        return cls(**values)


def _unconfigured(pack_name: str, slot: str) -> str:
    """The refusal for a discovered form slot whose config names no value.

    Half a configuration is worse than none here: the pack has said the portal
    demands this field, and nothing has said what belongs in it. Filling it
    anyway would mean inventing a value that lands on a patient's chart.
    """
    return (
        f"destination pack {pack_name!r} discovered selector slot {slot!r} but its "
        f"config names no value for it"
    )


def _render_date(fmt: str, value: date) -> str:
    """Render ``value`` through a ``%m/%d/%Y``-style template, from date parts.

    Supports ``%m``/``%d``/``%Y``/``%y`` (zero-padded) and ``%-m``/``%-d``
    (unpadded), built BY HAND — never platform ``strftime``, whose
    ``%-d``/``%-m`` are glibc-only and this runs on Windows CI too.
    """
    out: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        # A trailing bare '%' is passed through literally.
        token = fmt[i : i + 3] if fmt[i + 1 : i + 2] == "-" else fmt[i : i + 2]
        out.append(_date_token(token, value))
        i += len(token)
    return "".join(out)


def _date_token(token: str, value: date) -> str:
    return {
        "%m": f"{value.month:02d}",
        "%-m": str(value.month),
        "%d": f"{value.day:02d}",
        "%-d": str(value.day),
        "%Y": str(value.year),
        "%y": f"{value.year % 100:02d}",
        "%%": "%",
    }.get(token, token)


def _date_parts(text: str) -> tuple[str, ...]:
    """The numeric runs of ``text``, leading zeros stripped, in order.

    Compares two renderings of one date without knowing the portal's own
    format: ``"1/19/2023"`` and ``"01/19/2023"`` both reduce to
    ``("1", "19", "2023")``; an empty readback reduces to ``()`` and matches
    nothing (fail-closed). Known limit: a widget that echoes a time alongside
    the date makes this refuse an upload it could have allowed — the side to
    be wrong on, over silently waving through a replaced date.
    """
    return tuple(part.lstrip("0") or "0" for part in re.findall(r"\d+", text))


@dataclass(frozen=True)
class BrowserPackConfig:
    """The non-selector knobs of a browser pack.

    ``patient_search_url`` of ``None`` means the operator navigates to the
    patient list themselves (some EHRs have no stable deep link).
    ``dob_format``/``upload_date_format`` are ``%m/%d/%Y``-style templates
    rendered by hand, never platform ``strftime`` (``%-d``/``%-m`` are
    glibc-only). They are kept separate: a portal's search and its filing
    dialog need not write a date the same way. A discovered ``upload_*``
    slot with a ``None`` config is a half-configured pack and refuses.
    """

    name: str
    patient_search_url: str | None = None
    dob_format: str = "%m/%d/%Y"
    search_by: Literal["name", "dob", "both"] = "both"
    result_match: Literal["exact_name_dob"] = "exact_name_dob"
    success_timeout_ms: int = 30000
    upload_category_label: str | None = None
    upload_status_label: str | None = None
    upload_provider_label: str | None = None
    upload_comment: str | None = None
    upload_date_format: str = "%m/%d/%Y"
    select_by: Literal["label", "value"] = "label"

    def render_dob(self, value: date) -> str:
        """Render ``value`` using ``dob_format`` from the integer date parts."""
        return _render_date(self.dob_format, value)

    def render_upload_date(self, value: date) -> str:
        """Render ``value`` for the upload form's date field.

        The same hand-rolled tokenizer as :meth:`render_dob`, against
        ``upload_date_format`` — the two dates a pack writes can differ in
        shape without either going through the platform's locale.
        """
        return _render_date(self.upload_date_format, value)


class BrowserPackDestination:
    """The aggregate :class:`~anastomosis.destinations.base.Destination`, generic.

    Built from a :class:`SelectorMap`, a :class:`PageLike`, and a
    :class:`BrowserPackConfig`; implements every role protocol the engine
    drives, one instance serving as its own session/resolver/banner/scanner/
    driver — the same single-object pattern as
    :class:`anastomosis.deliver.browser.fake.FakeDestination`.
    """

    def __init__(
        self,
        selectors: SelectorMap,
        page: PageLike,
        config: BrowserPackConfig,
        *,
        teardown: Callable[[], None] | None = None,
    ) -> None:
        self._selectors = selectors
        self._page = page
        self._config = config
        # Releases OUR Playwright driver + CDP connection on close() — NEVER the
        # operator's browser. None in standalone/test use (no owned resources).
        self._teardown = teardown
        # The patient the banner readback last CONFIRMED, held only so the
        # upload dialog can be asked the same question a second time from
        # inside itself. The engine banner-checks immediately before every
        # upload, so this is never stale by more than one step; a failed check
        # clears it, and an upload that needs it and finds None refuses.
        self._verified_patient: Patient | None = None

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
    # In CDP mode we attach to a browser the OPERATOR launched; we never own
    # its lifecycle, so open()/close() are no-ops. close() also doubles as the
    # manager's per-recycle hook (every N uploads), so it must not tear down
    # our Playwright driver mid-run. Owned-resource teardown is release().

    def open(self) -> None:
        return None

    def close(self) -> None:
        # Still a no-op on the page. It does drop the banner's confirmation,
        # though: a recycled session may come back on a different page, and a
        # confirmation that outlived the session it was made in is the one
        # thing that could wave a dialog through unchecked.
        self._verified_patient = None

    def release(self) -> None:
        """Release OUR owned Playwright driver + CDP connection — once, at run end.

        Contract: distinct from :meth:`close` (per-recycle). ``browser.close()``
        only DISCONNECTS a ``connect_over_cdp`` browser (operator's Chrome
        keeps running), then ``playwright.stop()`` ends the driver subprocess.
        Best-effort: a hiccup is logged by type, never masks the run outcome.
        """
        if self._teardown is None:
            return
        try:
            self._teardown()
        except Exception as exc:  # teardown is best-effort; never mask the run result
            logger.warning("CDP teardown failed (%s)", exc_tag(exc))

    def is_alive(self) -> bool:
        return not self._page.is_closed()

    # --- PatientResolver ---

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        """Search the destination and return the EXACTLY-matched patient row.

        Contract: matches by name AND DOB (``exact_name_dob``, the only v1
        mode). Zero matches -> ``None``; exactly one -> opens the row and
        returns a :class:`DestinationPatient` (id = a hash of the row text);
        more than one -> :class:`PermanentDeliveryError` (RULES.md 12).

        PHI: logs slot names and the match COUNT only.
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
        # Open the MATCHED chart so the banner readback confirms the right
        # patient. Clicking the bare selector would open row 0 regardless of
        # which row matched — a wrong-chart open when the match is not row 0.
        self._page.click(self._selectors.patient_result_row, nth=index)
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

        ``search_by`` chooses name, DOB, or both (single input; a UI that
        splits them into two boxes is a v2 shape). PHI: never logged.
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

        Both name and DOB slots must match; any miss returns ``False`` (the
        engine raises :class:`~anastomosis.deliver.browser.errors.WrongPatientError`
        and aborts the run). PHI: logs the boolean outcome and slot names only.
        """
        banner_name = self._page.text_content(self._selectors.patient_banner_name) or ""
        banner_dob = self._page.text_content(self._selectors.patient_banner_dob) or ""
        name_ok = self._name_present(banner_name, expected)
        dob_ok = self._dob_present(banner_dob, expected)
        matches = name_ok and dob_ok
        # Remember WHO was confirmed, so an upload dialog that prefills a
        # patient can be checked against the same expectation. A miss clears
        # it: the engine is about to abort, and a stale confirmation left
        # behind would be the one thing able to wave the next upload through.
        self._verified_patient = expected if matches else None
        logger.info(
            "banner check: name_ok=%s dob_ok=%s (slots patient_banner_name/dob)",
            name_ok,
            dob_ok,
        )
        return matches

    # --- ExistingDocsScanner ---

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        """Return the document titles/filenames the destination already shows.

        The fingerprint is the title/filename as rendered
        (:attr:`UploadItem.fingerprint` defaults to the file name). PHI:
        logs the count only — a document title can embed a patient name.
        """
        texts = self._page.query_selector_all_text(self._selectors.documents_list_item)
        prints = {t.strip() for t in texts if t.strip()}
        logger.info("scanner: %d existing document fingerprint(s)", len(prints))
        return prints

    # --- UploadDriver ---

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """File ``item`` into the open chart through the upload UI.

        Contract: optional tab/open-button clicks, set the file input, fill
        whatever upload-form slots the pack discovered, submit, wait for the
        success marker — a timeout there is
        :class:`~anastomosis.deliver.browser.errors.TransientDeliveryError`
        (retryable). Returns a receipt with no doc id or size: browser
        uploads rarely echo either, and L6 read-back is the verifier's job.
        PHI: logs the item key and slot names only.
        """
        if self._selectors.documents_tab:
            self._page.click(self._selectors.documents_tab)
        if self._selectors.upload_open_button:
            self._page.click(self._selectors.upload_open_button)
        self._page.set_input_files(self._selectors.upload_file_input, str(item.file_path))
        self._fill_upload_form(item)
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
                "upload success marker not seen for item %s (%s)",
                safe_log_id(item.item_key),
                exc_tag(exc),
            )
            raise TransientDeliveryError(
                "upload success marker not observed within timeout"
            ) from exc
        logger.info(
            "upload filed item %s (slot upload_success_marker seen)", safe_log_id(item.item_key)
        )
        # Browser uploads rarely echo a doc id or size — L6 read-back verifies.
        return UploadReceipt(destination_doc_id=None, echoed_size_bytes=None)

    # --- the upload form (PHI-safe: slot names and booleans only) ---

    def _fill_upload_form(self, item: UploadItem) -> None:
        """Fill the upload form's fields, then read back the two that must be right.

        Every slot is optional and skipped when unset (RULES.md 13). Two are
        GATES that fail PERMANENTLY, not transiently: the echoed document
        date must match what was given, and the dialog's prefilled patient
        must still be who the banner confirmed — asked again from inside the
        dialog, the only check that can see it disagree with the chart.

        PHI: only slot names and booleans reach a log or exception message.
        """
        sel = self._selectors
        if sel.upload_filename_input:
            # The destination renders this string in its documents list, and
            # the duplicate scan compares that list against item.fingerprint —
            # so typing the fingerprint is what lets a resumed run recognise a
            # document it already filed instead of filing it twice.
            self._page.fill(self._form_selector(sel.upload_filename_input), item.fingerprint)
        if sel.upload_category_select:
            self._choose(
                sel.upload_category_select,
                self._config.upload_category_label,
                slot="upload_category_select",
            )
        if sel.upload_status_select:
            self._choose(
                sel.upload_status_select,
                self._config.upload_status_label,
                slot="upload_status_select",
            )
        if sel.upload_date_input:
            self._type_document_date(item)
        if sel.upload_patient_prefill:
            self._check_dialog_patient()
        if sel.upload_provider_select:
            self._choose(
                sel.upload_provider_select,
                self._config.upload_provider_label,
                slot="upload_provider_select",
            )
        if sel.upload_comments_input:
            if self._config.upload_comment is None:
                raise PermanentDeliveryError(_unconfigured(self.name, "upload_comments_input"))
            self._page.fill(
                self._form_selector(sel.upload_comments_input), self._config.upload_comment
            )

    def _choose(self, selector: str, label: str | None, *, slot: str) -> None:
        """Pick the configured option in one of the form's dropdowns.

        A dropdown refusing the configured choice is a structural mismatch —
        stop, rather than leave it at "Please select" and file anyway. The
        message names the SLOT, never the choice: a provider's name is a person.
        """
        if label is None:
            raise PermanentDeliveryError(_unconfigured(self.name, slot))
        try:
            self._page.select_option(
                self._form_selector(selector),
                label,
                by_label=self._config.select_by == "label",
            )
        except DeliveryError:
            # Already carries the engine's routing semantics — never re-wrap.
            raise
        except Exception as exc:
            logger.error(
                "upload form: slot %s would not take its configured choice (%s)",
                slot,
                exc_tag(exc),
            )
            raise PermanentDeliveryError(
                f"upload form slot {slot!r} does not offer the configured choice"
            ) from exc

    def _type_document_date(self, item: UploadItem) -> None:
        """Type the item's date of service, then confirm the form kept it.

        No date of service against a discovered date field is a refusal, not
        a blank: a chart filed under the form's default date is misfiled in
        the way hardest to notice later.
        """
        if item.date_of_service is None:
            raise PermanentDeliveryError(
                f"destination pack {self.name!r} discovered selector slot "
                f"'upload_date_input' but this item carries no date of service"
            )
        selector = self._form_selector(self._selectors.upload_date_input)
        typed = self._config.render_upload_date(item.date_of_service)
        self._page.fill(selector, typed)
        echoed = self._page.input_value(selector) or ""
        if _date_parts(echoed) != _date_parts(typed):
            logger.error("upload form: slot upload_date_input did not echo back what it was given")
            raise PermanentDeliveryError(
                "upload form slot 'upload_date_input' did not echo back the date it was given"
            )

    def _check_dialog_patient(self) -> None:
        """Confirm the dialog's prefilled patient is the one the banner confirmed.

        Fails closed both ways: a readback naming the wrong patient raises
        :class:`~anastomosis.deliver.browser.errors.WrongPatientError` (the
        run aborts); no confirmed patient to compare against is itself a
        refusal.
        """
        expected = self._verified_patient
        if expected is None:
            raise PermanentDeliveryError(
                f"destination pack {self.name!r} discovered selector slot "
                f"'upload_patient_prefill' but no banner readback confirmed a patient"
            )
        prefilled = self._page.input_value(
            self._form_selector(self._selectors.upload_patient_prefill)
        )
        matches = self._name_present(prefilled or "", expected)
        logger.info("upload form: dialog patient matches banner=%s", matches)
        if not matches:
            raise WrongPatientError(
                "upload dialog prefilled a different patient than the banner confirmed "
                "(slot upload_patient_prefill)"
            )

    @staticmethod
    def _form_selector(selector: str, index: int = _SINGLE_ROW_INDEX) -> str:
        """Render a form slot's ``{idx}`` row token for the row being filled."""
        return selector.replace(_ROW_INDEX_TOKEN, str(index))

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
        """Whether every declared name FIELD appears in ``text`` contiguously.

        Boundary-anchored (:func:`anastomosis.core.identity.name_parts_present`,
        RULES.md 6): each field matches as ONE contiguous phrase, so a
        multi-word surname cannot pass reordered word-by-word. Empty name
        parts fail closed to ``False``.
        """
        return name_parts_present(self._name_parts(patient), text)

    def _dob_present(self, text: str, patient: Patient) -> bool:
        """Whether the rendered DOB appears in ``text`` as a whole token.

        Boundary-anchored (:func:`anastomosis.core.identity.date_token_present`,
        RULES.md 6) so an unpadded DOB does not match inside a longer run.
        """
        dob = self._render_dob(patient)
        if not dob:
            # No DOB to match means the exact-name-dob contract cannot be met:
            # fail closed rather than match on name alone (a name collision is
            # exactly what the DOB gate defends against).
            return False
        return date_token_present(dob, text)

    def _row_matches(self, row_text: str, patient: Patient) -> bool:
        """A result row matches when BOTH name parts AND DOB are present in it."""
        return self._name_present(row_text, patient) and self._dob_present(row_text, patient)
