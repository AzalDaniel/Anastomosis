"""Chart reconstruction: template packs render canonical records to PDF.

The pack registry and rendering engine (browser lifecycle, collision
handling, idempotent skip) both live here.
"""

from .packs import (
    LoadedPack,
    PackManifest,
    PackStatus,
    SectionFlag,
    discover_packs,
    user_packs_dir,
)
from .packtrust import PackTrust, default_pack_trust, pack_content_hash, user_pack_trust_path

__all__ = [
    "LoadedPack",
    "PackManifest",
    "PackStatus",
    "PackTrust",
    "SectionFlag",
    "default_pack_trust",
    "discover_packs",
    "pack_content_hash",
    "user_pack_trust_path",
    "user_packs_dir",
]
