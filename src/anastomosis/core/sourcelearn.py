"""Learn a source format from one example file (the authoring half of W2).

The interpreter (:mod:`anastomosis.sources.learned.interpreter`) *executes* a
mapping; this module *proposes* one. Given a single example export it:

1. detects the file format and a stable column fingerprint
   (:func:`detect_format`);
2. profiles each column LOCALLY — counts, distinctness, an inferred type, and a
   PHI-safe masked shape — never echoing a raw value (:func:`profile_columns`);
3. ranks canonical target fields for each column through a pluggable
   :class:`CandidateScorer` (the alpha's :class:`FuzzyNameScorer` uses name
   similarity + a shipped synonym table + type affinity — no ML, no network);
4. assembles the operator's confirmed choices into a validated
   :class:`~anastomosis.sources.learned.spec.MappingSpec`, round-trips it
   against the example to PROVE no column is silently dropped, and saves it.

The scorer is a Protocol seam: a future local-embedding or schema-only hosted
scorer slots in here with no change to the wizard or interpreter. None ship in
the alpha — the deterministic matcher plus the mandatory human-confirm step is
the whole safety story.

PHI (the load-bearing rule of this module): inference runs entirely locally and
NO patient value ever leaves it — not to a log, not to an event, not into the
analysis summary. The only strings that surface are column NAMES, inferred type
labels, counts, and masked shapes. The mask allow-lists the separators it keeps
rather than deny-listing the characters it hides, so a script nobody thought of
is masked rather than shown.
"""

from __future__ import annotations

import csv
import json
import os
import re
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from anastomosis.core.model_paths import canonical_target_paths
from anastomosis.core.textutil import clean_cell
from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
from anastomosis.sources.learned.reader import (
    header_fingerprint,
    normalize_column,
    read_columns,
    read_rows,
)
from anastomosis.sources.learned.spec import (
    FieldMapping,
    Grouping,
    MappingError,
    MappingSpec,
    SourceFormat,
)

__all__ = [
    "CandidateScorer",
    "ColumnProfile",
    "FieldSuggestion",
    "FuzzyNameScorer",
    "RoundTripReport",
    "SourceAnalysis",
    "analyze_source",
    "build_mapping",
    "detect_format",
    "profile_columns",
    "round_trip",
    "save_mapping",
]

# --- format detection ----------------------------------------------------------

_SUFFIX_TYPES = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".ndjson": "ndjson",
    ".jsonl": "ndjson",
}
_ENCODINGS = ("utf-8-sig", "latin-1")
_UNSUPPORTED_STRUCTURED_SUFFIXES = frozenset({".xml", ".html", ".htm", ".pdf", ".zip"})
_BINARY_SIGNATURES = (b"%PDF-", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _reject_non_tabular_source(path: Path) -> bytes:
    """Fail closed for files that cannot be a flat learned-source export.

    Suffixes are a useful first gate, but content decides extensionless input.
    In particular, an XML C-CDA with an arbitrary suffix must never fall through
    the CSV sniffer as a one-column "table". Diagnostics intentionally identify
    only the file and format class, never its contents.
    """
    if path.suffix.lower() in _UNSUPPORTED_STRUCTURED_SUFFIXES:
        raise MappingError(f"source example {path} is not a supported flat structured export")
    try:
        # ``Path.read_bytes()[:65536]`` still allocates the entire file before
        # slicing. Source examples may be multi-gigabyte exports, so probe only
        # the bounded prefix we need for signatures and markup detection.
        with path.open("rb") as handle:
            sample = handle.read(65536)
    except OSError as exc:
        raise MappingError(f"cannot read source example {path}: {type(exc).__name__}") from exc
    stripped = sample.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    binary_signature = any(
        stripped.startswith(signature.lower()) for signature in _BINARY_SIGNATURES
    )
    if b"\x00" in sample or binary_signature:
        raise MappingError(f"source example {path} is binary, not a flat structured export")
    if stripped.startswith((b"<?xml", b"<!doctype html", b"<html", b"<")):
        raise MappingError(f"source example {path} is markup, not a flat structured export")
    return sample


def _detect_encoding(sample: bytes) -> str:
    """Pick the first encoding that decodes a bounded prefix (best effort)."""
    for encoding in _ENCODINGS:
        try:
            sample.decode(encoding)
        except (UnicodeError, LookupError):
            continue
        return encoding
    return "latin-1"  # latin-1 decodes any byte; the loop's last entry, defensively


def detect_format(path: Path) -> SourceFormat:
    """Detect type/delimiter/encoding/columns/fingerprint for one example file."""
    if not path.is_file():
        raise MappingError(f"source example {path} is not a file")
    sample = _reject_non_tabular_source(path)
    encoding = _detect_encoding(sample)
    suffix = path.suffix.lower()
    file_type = _SUFFIX_TYPES.get(suffix)
    delimiter: str | None = None
    if file_type is None:
        file_type = _sniff_type(path, encoding)
    if file_type == "csv":
        delimiter = _sniff_delimiter(path, encoding)
    provisional = SourceFormat(
        type=file_type,  # type: ignore[arg-type]  # _sniff_type returns a valid literal
        delimiter=delimiter,
        encoding=encoding,
        header_fingerprint="",
        columns=["_"],
    )
    columns = read_columns(path, provisional)
    if not columns:
        raise MappingError(f"source example {path} has no columns")
    return SourceFormat(
        type=file_type,  # type: ignore[arg-type]
        delimiter=delimiter,
        encoding=encoding,
        header_fingerprint=header_fingerprint(columns),
        columns=columns,
    )


def _sniff_type(path: Path, encoding: str) -> str:
    text = path.read_text(encoding=encoding)
    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        try:
            json.loads(text)
            return "json"
        except ValueError:
            pass
    first = stripped.splitlines()[0] if stripped.splitlines() else ""
    try:
        json.loads(first)
        return "ndjson"
    except ValueError:
        pass
    return "tsv" if "\t" in first else "csv"


def _sniff_delimiter(path: Path, encoding: str) -> str:
    sample = path.read_text(encoding=encoding)[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
    except csv.Error:
        return ","


# --- column profiling (PHI-safe) ----------------------------------------------

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"^\d{3}-?\d{2}-?\d{4}$")),
    ("email", re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")),
    ("phone", re.compile(r"^\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")),
    ("zip", re.compile(r"^\d{5}(-\d{4})?$")),
    ("loinc", re.compile(r"^\d{1,5}-\d$")),
    ("icd10", re.compile(r"^[A-TV-Z]\d{2}(\.\w{1,4})?$")),
    ("datetime", re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d|\d{1,2}/\d{1,2}/\d{2,4}\s+\d")),
    ("date", re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}/\d{1,2}/\d{2,4}$")),
)
_TYPE_THRESHOLD = 0.6


@dataclass(frozen=True)
class ColumnProfile:
    """PHI-safe per-column statistics (names/types/counts/masked shapes only)."""

    name: str
    normalized: str
    non_null: int
    distinct: int
    inferred_type: str
    sample_shape: str


#: The only characters a masked shape may show as themselves: the ASCII
#: separators that give a value its form. Everything else is content until
#: proven otherwise, which is why this is an allow-list. A deny-list of
#: "letters and digits" was the bug: `[A-Za-z]` and `[0-9]` are ASCII-only, so
#: a name in any other script — CJK, Cyrillic, Arabic, Greek, Hebrew, kana,
#: Hangul — passed through the mask unchanged and was printed to the console
#: under the words "PHI-safe — no values shown".
_SHAPE_PUNCTUATION = frozenset(" \t!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _mask(value: str) -> str:
    """A digit/letter-masked shape of a value — carries form, never content.

    Fails closed: a character this function does not recognise as a separator
    becomes `A`, so an unfamiliar script is masked rather than shown.
    """
    shape = [
        char if char in _SHAPE_PUNCTUATION else "N" if char.isdigit() or char.isnumeric() else "A"
        for char in value
    ]
    return "".join(shape)[:24]


def _infer_type(values: Sequence[str]) -> str:
    if not values:
        return "empty"
    sample = values[:200]
    counts: dict[str, int] = {}
    numeric = 0
    for value in sample:
        for label, pattern in _PATTERNS:
            if pattern.match(value):
                counts[label] = counts.get(label, 0) + 1
                break
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            numeric += 1
    if counts:
        best, hits = max(counts.items(), key=lambda kv: kv[1])
        if hits / len(sample) >= _TYPE_THRESHOLD:
            return best
    if numeric / len(sample) >= _TYPE_THRESHOLD:
        return "numeric"
    return "text"


def profile_columns(
    rows: Iterable[dict[str, str | None]], columns: Sequence[str]
) -> list[ColumnProfile]:
    """Profile each column locally — counts, distinctness, type, masked shape."""
    materialized = list(rows)
    profiles: list[ColumnProfile] = []
    for column in columns:
        values = [v for row in materialized if (v := clean_cell(row.get(column))) is not None]
        profiles.append(
            ColumnProfile(
                name=column,
                normalized=normalize_column(column),
                non_null=len(values),
                distinct=len(set(values)),
                inferred_type=_infer_type(values),
                sample_shape=_mask(values[0]) if values else "",
            )
        )
    return profiles


# --- candidate scoring (the pluggable seam) -----------------------------------


class CandidateScorer(Protocol):
    """Ranks canonical target paths for one column. The future-extension seam."""

    def score(self, profile: ColumnProfile, targets: frozenset[str]) -> list[tuple[str, float]]:
        """Return ``(target_path, score in 0..1)`` pairs, best first."""
        ...


# Inferred type -> the targets it strongly suggests (a scoring bonus, not a gate).
_TYPE_AFFINITY: dict[str, set[str]] = {
    "email": {"patient.email"},
    "ssn": {"patient.ssn"},
    "phone": {
        "patient.phone_home",
        "patient.phone_mobile",
        "patient.phone_work",
        "patient.phone_other",
    },
    "zip": {"patient.address.postal_code"},
    "date": {"patient.birth_date", "encounter.date_of_service"},
    "datetime": {"encounter.signed_at", "encounter.last_modified_at", "encounter.date_of_service"},
}
_SYNONYMS_PATH = Path(__file__).resolve().parent.parent / "sources" / "learned" / "synonyms.json"


def _load_synonyms() -> dict[str, list[str]]:
    try:
        data = json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


class FuzzyNameScorer:
    """The alpha scorer: name similarity + synonyms + type affinity. No ML/network.

    rapidfuzz is imported lazily (only ``anast source init`` constructs this), so
    the rest of the toolkit never pays for it at import time.
    """

    def __init__(self, synonyms: dict[str, list[str]] | None = None) -> None:
        from rapidfuzz import fuzz  # lazy: only the source-learn path needs it

        # token_sort_ratio (not token_set_ratio): order-insensitive but
        # length-SENSITIVE, so a generic synonym like "phone" does not score a
        # perfect subset match against "mobile phone" and steal the home slot.
        self._ratio = fuzz.token_sort_ratio
        self._synonyms = synonyms if synonyms is not None else _load_synonyms()

    def _candidates(self, target: str) -> list[str]:
        leaf = target.split(".", 1)[1].replace(".", " ").replace("_", " ")
        return [normalize_column(c) for c in [leaf, *self._synonyms.get(target, [])]]

    def score(self, profile: ColumnProfile, targets: frozenset[str]) -> list[tuple[str, float]]:
        affinity = _TYPE_AFFINITY.get(profile.inferred_type, set())
        results: list[tuple[str, float]] = []
        for target in targets:
            best = max(self._ratio(profile.normalized, c) for c in self._candidates(target))
            name_sim = best / 100.0
            # A non-saturating blend: type affinity nudges the right phone/date
            # slot without erasing the NAME signal that distinguishes home vs
            # mobile (which a saturating ``min(1, +bonus)`` would flatten to a tie).
            score = 0.85 * name_sim + (0.15 if target in affinity else 0.0)
            results.append((target, score))
        # Deterministic order: by score, then target path (targets is a set, so
        # an undefined tie-break would make suggestions non-reproducible).
        results.sort(key=lambda item: (-item[1], item[0]))
        return results


# --- analysis -> suggestions --------------------------------------------------

_MAP_THRESHOLD = 0.6
_TRANSFORM_BY_TARGET = {
    "patient.birth_date": "parse_date",
    "encounter.date_of_service": "parse_date",
    "encounter.signed_at": "parse_datetime",
    "encounter.last_modified_at": "parse_datetime",
    "patient.phone_home": "phone",
    "patient.phone_mobile": "phone",
    "patient.phone_work": "phone",
    "patient.phone_other": "phone",
}
_PATIENT_KEY_HINTS = ("patient id", "patient", "mrn", "guid", "chart", "member id", "record")
_ENCOUNTER_KEY_HINTS = ("encounter", "visit", "appointment", "note id", "case")


@dataclass(frozen=True)
class FieldSuggestion:
    """One column's best canonical target (or none), with alternatives."""

    source_path: str
    target_path: str | None
    transform: str
    confidence: float
    alternatives: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class SourceAnalysis:
    """The PHI-safe analysis a wizard presents for confirmation."""

    fmt: SourceFormat
    profiles: list[ColumnProfile]
    suggestions: list[FieldSuggestion]
    patient_key: str | None
    encounter_key: str | None
    row_scope: str

    def summary_lines(self) -> list[str]:
        """Human-readable, PHI-safe lines (names/types/counts/shapes only)."""
        lines = [
            f"format: {self.fmt.type} ({len(self.fmt.columns)} columns), "
            f"patient_key={self.patient_key!r}, encounter_key={self.encounter_key!r}, "
            f"row_scope={self.row_scope}",
        ]
        for suggestion in self.suggestions:
            profile = next(p for p in self.profiles if p.name == suggestion.source_path)
            if suggestion.source_path == self.patient_key:
                target = "(patient key)"
            elif suggestion.source_path == self.encounter_key:
                target = suggestion.target_path or "(encounter key)"
            else:
                target = suggestion.target_path or "(unmapped -> extensions)"
            lines.append(
                f"  {suggestion.source_path}  [{profile.inferred_type}, "
                f"{profile.non_null} values, {profile.distinct} distinct, "
                f"shape {profile.sample_shape!r}] "
                f"-> {target}  ({suggestion.confidence:.0%})"
            )
        return lines


def _suggest_fields(
    profiles: list[ColumnProfile],
    scorer: CandidateScorer,
    targets: frozenset[str],
    reserved: set[str],
) -> list[FieldSuggestion]:
    suggestions: list[FieldSuggestion] = []
    taken: set[str] = set()
    for profile in profiles:
        ranked = [pair for pair in scorer.score(profile, targets) if pair[0] not in taken]
        best_target, best_score = ranked[0] if ranked else (None, 0.0)
        if profile.name in reserved or best_target is None or best_score < _MAP_THRESHOLD:
            suggestions.append(FieldSuggestion(profile.name, None, "strip", best_score, ranked[:3]))
            continue
        taken.add(best_target)
        transform = _TRANSFORM_BY_TARGET.get(best_target, "strip")
        suggestions.append(
            FieldSuggestion(profile.name, best_target, transform, best_score, ranked[1:4])
        )
    return suggestions


def _infer_grouping(profiles: list[ColumnProfile]) -> tuple[str | None, str | None, str]:
    by_name = {p.name: p for p in profiles}

    def best_hint(hints: tuple[str, ...], among: list[ColumnProfile] | None = None) -> str | None:
        pool = among if among is not None else profiles
        return next(
            (p.name for p in pool if any(h in p.normalized for h in hints)),
            None,
        )

    patient_key = best_hint(_PATIENT_KEY_HINTS)
    if patient_key is None and profiles:
        patient_key = profiles[0].name  # operator confirms; first column is the fallback
    # An encounter key must be a stable identifier, never a date — a date column
    # is not unique per encounter, so keying on it would silently collapse two
    # distinct visits charted on the same day. A date is mapped to
    # ``encounter.date_of_service`` instead, and each row is its own encounter.
    id_like = [p for p in profiles if p.inferred_type not in ("date", "datetime")]
    encounter_key = best_hint(_ENCOUNTER_KEY_HINTS, id_like)
    if encounter_key == patient_key:
        encounter_key = None
    # Data-driven grain: a patient key that repeats (distinct < non_null) means
    # multiple rows per patient -> encounter-grained.
    row_scope = "patient"
    if patient_key and (profile := by_name.get(patient_key)):
        if profile.distinct < profile.non_null:
            row_scope = "encounter"
    if encounter_key is not None:
        row_scope = "encounter"
    return patient_key, encounter_key, row_scope


def analyze_source(path: Path, *, scorer: CandidateScorer | None = None) -> SourceAnalysis:
    """Detect, profile, and propose a mapping for one example file (PHI-safe)."""
    fmt = detect_format(path)
    rows = read_rows(path, fmt)
    profiles = profile_columns(rows, fmt.columns)
    patient_key, encounter_key, row_scope = _infer_grouping(profiles)
    used_scorer = scorer if scorer is not None else FuzzyNameScorer()
    # The patient key is THE patient id (it becomes the id + a SOURCE_GUID
    # identifier), so it is never also a field mapping. The encounter key may
    # still map to a field (e.g. a visit-date column → encounter.date_of_service).
    reserved = {patient_key} if patient_key is not None else set()
    suggestions = _suggest_fields(profiles, used_scorer, canonical_target_paths(), reserved)
    return SourceAnalysis(fmt, profiles, suggestions, patient_key, encounter_key, row_scope)


# --- build / round-trip / save ------------------------------------------------


def build_mapping(
    analysis: SourceAnalysis,
    *,
    mapping_id: str,
    display: str,
    decisions: dict[str, tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> MappingSpec:
    """Assemble a reviewed :class:`MappingSpec` from confirmed decisions.

    ``decisions`` maps a source column to ``(target_path, transform)``; when
    omitted, the analysis suggestions are accepted as-is. Columns with no
    decision (and no suggestion) are recorded as ``unmapped_source_fields`` —
    still preserved in ``extensions`` by the interpreter, never dropped.
    """
    chosen: dict[str, tuple[str, str]] = {}
    if decisions is not None:
        chosen = dict(decisions)
    else:
        for suggestion in analysis.suggestions:
            if suggestion.target_path is not None:
                chosen[suggestion.source_path] = (suggestion.target_path, suggestion.transform)
    field_mappings = [
        FieldMapping(
            source_path=source,
            target_path=target,
            transform=transform,
            confidence=next(
                (s.confidence for s in analysis.suggestions if s.source_path == source), 1.0
            ),
            human_confirmed=True,
        )
        for source, (target, transform) in chosen.items()
    ]
    reserved = {k for k in (analysis.patient_key, analysis.encounter_key) if k is not None}
    unmapped = [c for c in analysis.fmt.columns if c not in chosen and c not in reserved]
    if analysis.patient_key is None:
        raise MappingError(
            "cannot build a mapping without a patient_key — confirm one in the wizard"
        )
    return MappingSpec(
        mapping_id=mapping_id,
        created_at=now or datetime.now(UTC),
        human_reviewed=True,
        display=display,
        source_format=analysis.fmt,
        grouping=Grouping(
            patient_key=analysis.patient_key,
            encounter_key=analysis.encounter_key,
            row_scope=analysis.row_scope,  # type: ignore[arg-type]  # validated literal
        ),
        field_mappings=field_mappings,
        unmapped_source_fields=unmapped,
    )


@dataclass(frozen=True)
class RoundTripReport:
    """The proof a mapping loses nothing: records built, columns all accounted for."""

    ok: bool
    record_count: int
    dropped_columns: list[str]
    error: str | None


def round_trip(spec: MappingSpec, example: Path) -> RoundTripReport:
    """Apply ``spec`` to its example and prove no UN-mapped value is dropped.

    Checking column *names* is not enough — a row-grain mismatch can collapse a
    populated column to a single last-value-wins cell while the column name is
    still "present". So this verifies per VALUE: every distinct, populated value
    of every un-mapped, non-key column must survive verbatim in some record's
    ``extensions``. A column whose values are not all preserved is reported (and
    the wizard refuses to save). Mapped columns are the operator's explicit
    transform choice (a sentinel or ``const`` may legitimately null a cell), so
    they are trusted; the lossless guarantee is about the columns left un-mapped.
    """
    adapter = LearnedSourceAdapter(spec)
    try:
        records = list(adapter.load(example))
    except MappingError as exc:
        return RoundTripReport(False, 0, [], str(exc))

    prefix = f"learned:{spec.mapping_id}:"
    preserved: dict[str, set[str | None]] = {}
    for record in records:
        for obj in (record.patient, *record.encounters):
            for key, value in obj.extensions.items():
                if key.startswith(prefix):
                    preserved.setdefault(key[len(prefix) :], set()).add(clean_cell(str(value)))

    mapped = {m.source_path for m in spec.field_mappings}
    keys = {spec.grouping.patient_key, spec.grouping.encounter_key}
    rows = read_rows(example, spec.source_format)
    dropped: list[str] = []
    for column in spec.source_format.columns:
        if column in mapped or column in keys:
            continue
        wanted = {clean_cell(row.get(column)) for row in rows} - {None}
        if wanted and not wanted <= preserved.get(column, set()):
            dropped.append(column)
    return RoundTripReport(not dropped, len(records), sorted(dropped), None)


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write ``text`` to ``path`` atomically and owner-only (temp + os.replace).

    Encoded here and written as BYTES, which is not a style preference. This
    helper writes ``mapping.json``, and the trust record beside it stores a
    sha256 of the string that was passed in — so the bytes that land on disk
    have to be that string and nothing else. Text mode with the default
    ``newline`` translates every ``\n`` to ``\r\n`` on Windows, which meant
    the digest described a file that had never existed: verification hashes
    what it reads, and on Windows that never matched. Every freshly saved
    mapping reported itself as edited since review, so the one warning that
    exists to catch a real post-review edit fired on all of them and told an
    operator nothing.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    if os.name == "posix":
        path.chmod(mode)


def _mapping_markdown(spec: MappingSpec) -> str:
    lines = [
        f"# Learned source: {spec.display}",
        "",
        f"- mapping id: `{spec.mapping_id}`",
        f"- format: {spec.source_format.type} ({len(spec.source_format.columns)} columns)",
        f"- patient key: `{spec.grouping.patient_key}`",
        f"- encounter key: `{spec.grouping.encounter_key}`",
        f"- row scope: {spec.grouping.row_scope}",
        f"- created: {spec.created_at.isoformat()}",
        "",
        "## Field mappings",
        "",
        "| source column | canonical field | transform |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| `{m.source_path}` | `{m.target_path}` | `{m.transform}` |" for m in spec.field_mappings
    ]
    lines += [
        "",
        "## Unmapped columns (preserved in `extensions`, never dropped)",
        "",
        *(f"- `{c}`" for c in spec.unmapped_source_fields),
        "",
        "## Honest limits",
        "",
        "- One flat file per format; no multi-file joins or per-row record-type dispatch.",
        "- The canonical target is always `PatientRecord`; clinical lists "
        "(observations, conditions, medications) are not mapped from a flat file in this version.",
        "- Edit this directory's `mapping.json` to refine the mapping, then re-run `source init` "
        "to re-verify; the toolkit warns if the file changed since it was reviewed.",
        "",
    ]
    return "\n".join(lines)


def save_mapping(spec: MappingSpec, base_dir: Path) -> Path:
    """Persist a reviewed mapping under ``base_dir/<mapping_id>/`` (owner-only).

    Refuses to write unless ``spec.human_reviewed`` is set — the data-only trust
    gate. Writes ``mapping.json`` (atomic, 0600), a human-readable ``MAPPING.md``,
    and a ``source_trust.json`` content hash that later WARNS (never blocks) if
    the mapping is hand-edited after review.
    """
    if not spec.human_reviewed:
        raise MappingError("refusing to save a mapping that was not human-reviewed")
    target_dir = base_dir / spec.mapping_id
    target_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        target_dir.chmod(stat.S_IRWXU)  # 0700 — owner only, like the other user state
    mapping_json = json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    _atomic_write(target_dir / "mapping.json", mapping_json, 0o600)
    _atomic_write(target_dir / "MAPPING.md", _mapping_markdown(spec), 0o600)
    import hashlib

    digest = hashlib.sha256(mapping_json.encode("utf-8")).hexdigest()
    trust = json.dumps({"mapping_sha256": digest, "human_reviewed": True}, indent=2) + "\n"
    _atomic_write(target_dir / "source_trust.json", trust, 0o600)
    return target_dir
