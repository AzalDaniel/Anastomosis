"""FHIR-API destination attach (the API delivery seam).

Single owner of the API-route construction: takes a FHIR R4 base URL and the
bearer token the frontend already resolved, and returns a live
:class:`~anastomosis.deliver.fhir_api.destination.FhirApiDestination` over a
stdlib-``urllib`` :class:`~anastomosis.deliver.fhir_api.client.FhirClient`.

This is the API counterpart to
:func:`anastomosis.deliver.browser.attach.attach_destination`, and it lives
here — not in ``cli.py`` — for the same reason: the CLI's ``anast upload
--fhir`` and any second frontend must reach it without importing a CLI-private
helper. ``anastomosis.cli`` re-publishes it as ``_make_fhir_destination`` so
tests can monkeypatch the whole seam and drive the upload flow with no server.

Two properties are deliberate:

* **The token is a PARAMETER, never read from the environment here.** The
  frontend owns the "which variable holds the token" policy (``anast upload
  --fhir-token-env``); this seam only carries the resolved value into
  :class:`FhirEndpoint`, which masks it in ``repr`` so it cannot reach a log
  line or a traceback frame. Passing it in argv would make it ``ps``-visible,
  which is exactly what the env-var indirection exists to prevent.
* **The base URL is validated here too.** :class:`FhirEndpoint` enforces
  https-or-loopback on construction, so even a caller that skipped the
  frontend's pre-flight gate cannot open a cleartext off-loopback session. The
  frontend still gates first, to turn the ``ValueError`` into a clean exit code
  instead of a traceback.
"""

from __future__ import annotations

from .client import FhirClient, FhirEndpoint
from .destination import FhirApiDestination

__all__ = ["DEFAULT_TOKEN_ENV", "attach_fhir_destination"]

# The environment variable ``anast upload --fhir`` reads the bearer token from
# by default. Named here (not in the CLI) so a second frontend and the docs
# quote ONE spelling of it.
DEFAULT_TOKEN_ENV = "ANAST_FHIR_TOKEN"  # noqa: S105 — a variable NAME, not a secret


def attach_fhir_destination(
    base_url: str,
    *,
    bearer_token: str | None = None,
    create_missing_patients: bool = False,
) -> FhirApiDestination:
    """Build the FHIR R4 destination for ``base_url`` (the SEAM tests mock).

    ``bearer_token`` is sent only in the ``Authorization`` header of a live
    request; ``None`` means unauthenticated (the normal case for a local HAPI
    server). ``create_missing_patients`` lets the resolver POST a new
    ``Patient`` when the destination holds none matching — the migration-target
    case, where the patients have not been moved over yet.

    The returned destination owns no browser and no subprocess, so it has no
    ``release()``; :func:`~anastomosis.core.upload_command.run_upload_command`
    duck-types that hook and simply skips it.
    """
    endpoint = FhirEndpoint(base_url, bearer_token=bearer_token)
    return FhirApiDestination(FhirClient(endpoint), create_missing_patients=create_missing_patients)
