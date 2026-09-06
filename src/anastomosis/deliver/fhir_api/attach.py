"""FHIR-API destination attach: the construction seam ``anast upload --fhir``
and any second frontend call (API counterpart to
:func:`anastomosis.deliver.browser.attach.attach_destination`).

``.client``/``.destination`` import lazily, inside the function, because both
reach :mod:`anastomosis.deliver.browser.errors` and drag in the whole
browser-upload package (RULES.md 75); ``DEFAULT_TOKEN_ENV`` stays a bare
constant so ``cli_commands/upload.py`` can import it alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .destination import FhirApiDestination

__all__ = ["DEFAULT_TOKEN_ENV", "attach_fhir_destination"]

# Named here, not in the CLI, so a second frontend quotes one spelling.
DEFAULT_TOKEN_ENV = "ANAST_FHIR_TOKEN"  # noqa: S105 — a variable NAME, not a secret


def attach_fhir_destination(
    base_url: str,
    *,
    bearer_token: str | None = None,
    create_missing_patients: bool = False,
    search_by_ssn: bool = False,
) -> FhirApiDestination:
    """Build the FHIR R4 destination for ``base_url`` (the seam tests mock).
    Contract: the token is a parameter only, never read from the environment;
    ``search_by_ssn`` is off by default (its query string reaches access
    logs). No ``release()``: callers duck-type the hook.
    """
    from .client import FhirClient, FhirEndpoint
    from .destination import FhirApiDestination

    endpoint = FhirEndpoint(base_url, bearer_token=bearer_token)
    return FhirApiDestination(
        FhirClient(endpoint),
        create_missing_patients=create_missing_patients,
        search_by_ssn=search_by_ssn,
    )
