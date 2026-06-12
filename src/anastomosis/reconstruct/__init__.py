"""Chart reconstruction: template packs render canonical records to PDF.

The pack registry lives here; the rendering engine (browser lifecycle,
collision handling, idempotent skip) arrives with the archive vertical
slice.
"""

from .packs import LoadedPack, PackManifest, PackStatus, SectionFlag, discover_packs

__all__ = ["LoadedPack", "PackManifest", "PackStatus", "SectionFlag", "discover_packs"]
