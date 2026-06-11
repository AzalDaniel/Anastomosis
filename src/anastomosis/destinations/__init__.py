"""Destination packs — see :mod:`.base` for the upload contract.

A destination pack implements the protocols in :mod:`.base` to teach the
browser delivery engine how to file one reconstructed chart into one
foreign EHR. Concrete packs (e.g. ``destinations/tebra``) land in M2; this
package ships the contract and the data types the engine drives.
"""

from .base import (
    BannerCheck,
    Destination,
    DestinationPatient,
    ExistingDocsScanner,
    PatientResolver,
    Session,
    UploadDriver,
    UploadItem,
    UploadReceipt,
)

__all__ = [
    "BannerCheck",
    "Destination",
    "DestinationPatient",
    "ExistingDocsScanner",
    "PatientResolver",
    "Session",
    "UploadDriver",
    "UploadItem",
    "UploadReceipt",
]
