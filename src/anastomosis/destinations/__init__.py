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
from .browserpack import (
    BrowserPackConfig,
    BrowserPackDestination,
    PackNotReadyError,
    PageLike,
    PlaywrightPageAdapter,
    SelectorMap,
)
from .loader import (
    BrowserPackError,
    LoadedBrowserPack,
    load_destination_pack,
    user_destinations_dir,
)
from .registry import (
    BrowserKind,
    Capability,
    CcdaImportKind,
    DestinationEntry,
    DestinationRegistry,
    DocWriteKind,
    Evidence,
)

__all__ = [
    "BannerCheck",
    "BrowserKind",
    "BrowserPackConfig",
    "BrowserPackDestination",
    "BrowserPackError",
    "Capability",
    "CcdaImportKind",
    "Destination",
    "DestinationEntry",
    "DestinationPatient",
    "DestinationRegistry",
    "DocWriteKind",
    "Evidence",
    "ExistingDocsScanner",
    "LoadedBrowserPack",
    "PackNotReadyError",
    "PageLike",
    "PatientResolver",
    "PlaywrightPageAdapter",
    "SelectorMap",
    "Session",
    "UploadDriver",
    "UploadItem",
    "UploadReceipt",
    "load_destination_pack",
    "user_destinations_dir",
]
