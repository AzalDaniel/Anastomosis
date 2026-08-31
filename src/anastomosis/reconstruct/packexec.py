"""What third-party pack code is handed when it runs — and what that is not.

A template pack ships ``context.py``, and loading the pack executes it. Until
this module existed, an external (``--pack-dir``) or per-user (taught) pack's
``context.py`` executed with the full authority of the desktop user who launched
the app: the process's open files, its network, its ability to spawn children,
its environment. That is the authority of the operator's own shell, granted by
default to a layout file that may have arrived by email — and it was a default
nobody had ever chosen.

**The decision.** Non-built-in pack code is executed against a restricted
globals mapping: a curated ``__builtins__`` with no ``open``, ``eval``,
``exec``, ``compile``, ``input``, ``globals`` or ``print``, and a gated
``__import__`` that admits only :data:`PACK_ALLOWED_MODULES` — the canonical
model, the pack-context helpers, and pure-computation stdlib. ``os``, ``sys``,
``subprocess``, ``socket``, ``pathlib``, ``shutil``, ``urllib``, ``importlib``,
``ctypes`` and everything else are refused by name, at import, as a
:class:`PackCapabilityRefused` that discovery reports as the pack's diagnosis.

**Why this shape.** A template pack's whole job is to turn one encounter and one
record into a dict of template variables. It has no reason to read a file, reach
the network, or start a process — and until it tried, nobody could see that it
could. Naming the capabilities here makes the pack API's surface something a
reader can check rather than something a reader must assume.

**What this is NOT.** This is not a security boundary, and it must not be
described as one anywhere. A restricted globals mapping is not a CPython
sandbox: pack code that wants ``os`` can still walk
``().__class__.__base__.__subclasses__()`` to a class that holds a reference to
it, or read it off an allowed module's own globals. Someone who writes a pack
specifically to escape this will escape it. What it does is remove the obvious
primitives and turn a silent capability into a loud, named refusal — so the
accidental and the casual (a helper pasted in from elsewhere, a pack that
fetches a font, a "temporary" debug write) stop working and say why.

The defense that does carry weight is the layer above: the operator's explicit
consent (``--allow-external-packs``) and the content-hash trust gate
(:mod:`anastomosis.reconstruct.packtrust`), which decide *whether* third-party
code runs at all and *which bytes* run. This module only decides what the code
those gates already admitted is handed.

**Built-ins are exempt.** ``anastomosis/packs/*`` ships inside the same wheel as
this module and already runs with the application's authority; restricting it
would restrict the application against itself, and the shipped Practice Fusion
layout legitimately reads its own placeholder logo asset off disk. Exemption is
by ORIGIN, decided in :mod:`anastomosis.reconstruct.packs`, not by anything the
pack can assert about itself.

**Filesystem, deliberately.** ``pathlib`` and ``open`` are refused, so an
external pack cannot read a file at all. A pack that must embed an asset ships
it as a ``data:`` URI in its manifest tokens — the route the built-in Practice
Fusion layout already honors through ``tokens.logo_data_uri`` — which keeps the
bytes inside the manifest the trust hash covers instead of in a file beside it
that nothing pins.

PHI: nothing patient-derived passes through this module. A refusal message
carries a module or builtin NAME only.
"""

from __future__ import annotations

import builtins
import importlib
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "PACK_ALLOWED_BUILTINS",
    "PACK_ALLOWED_MODULES",
    "PackCapabilityRefused",
    "restrict_module",
]


class PackCapabilityRefused(ImportError):
    """Pack code asked for a capability the pack API does not grant.

    An :class:`ImportError` so the loader's existing defensive ``except`` in
    :mod:`anastomosis.reconstruct.packs` diagnoses it like any other bad pack —
    the pack comes back unavailable, with a message naming the module it asked
    for, and the rest of the packs load unaffected.
    """


#: The modules a pack's ``context.py`` may import, as exact names or package
#: roots (a name matches when it equals an entry or is a submodule of one).
#:
#: The anastomosis entries are the pack API itself: the canonical model a
#: builder reads, the code/date/text helpers it formats with, the shared
#: context helpers, and the ONE shipped context a taught layout's generated
#: ``context.py`` delegates to — refusing that would refuse every layout the
#: Teach writes. It is that one module and not the ``anastomosis.packs``
#: package root, which was the first spelling: a submodule match let a pack
#: write ``from anastomosis.packs.practice_fusion_soap.context import Path``
#: and get ``pathlib.Path`` — arbitrary read and write — straight back, while
#: the table below still truthfully said ``pathlib`` was not on it. A
#: capability reachable in one import is not withheld, whatever the table
#: says. The stdlib entries are pure computation: no filesystem, no network,
#: no process, no interpreter introspection. Anything absent is refused by
#: name.
PACK_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # the pack API
        "anastomosis.core.codes",
        "anastomosis.core.model",
        "anastomosis.core.textutil",
        "anastomosis.core.timeutil",
        "anastomosis.packs.generic_soap.context",
        "anastomosis.reconstruct.packctx",
        # pure-computation stdlib
        "__future__",
        "abc",
        "base64",
        "bisect",
        "collections",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "html",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
        "zoneinfo",
    }
)

# ``time`` is on that list for a reason no reader would guess from pack code:
# ``date.strftime`` is implemented in C and reaches for the ``time`` module
# through the CALLING frame's ``__import__`` — the pack's, here — so withholding
# it turns every formatted date in every non-built-in layout into a refusal.
# Found the way such things are found: a copied layout that rendered six charts
# a moment earlier failed all six, on the line that formats a date of birth. It
# is pure computation and grants nothing ``datetime`` does not already.

#: The builtin names a pack's module globals carry. Everything a builder needs
#: to compute over a record, and nothing that reaches past the process:
#:
#: * no ``open`` — see the filesystem note in the module docstring;
#: * no ``eval``/``exec``/``compile`` — code the trust hash never covered;
#: * no ``input`` — a render batch has no terminal to block on;
#: * no ``print`` — a pack that prints puts patient values on the process's
#:   stdout, which is where the log redaction filter cannot reach them;
#: * no ``globals``/``locals``/``vars``/``dir``/``id`` — introspection the pack
#:   API has no use for. (These make the obvious escapes less convenient. They
#:   do not close them; see the module docstring.)
#:
#: ``__build_class__`` is here because a ``class`` statement (a ``@dataclass``
#: view object, say) does not compile without it.
PACK_ALLOWED_BUILTINS: frozenset[str] = frozenset(
    {
        "__build_class__",
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "callable",
        "chr",
        "classmethod",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "object",
        "ord",
        "property",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "slice",
        "sorted",
        "staticmethod",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "zip",
        # constants
        "Ellipsis",
        "NotImplemented",
        # the exceptions a builder raises or catches
        "ArithmeticError",
        "AttributeError",
        "Exception",
        "IndexError",
        "KeyError",
        "LookupError",
        "NotImplementedError",
        "RuntimeError",
        "StopIteration",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
    }
)

# Resolved once, at import of THIS module, so a name that is not a builtin fails
# loudly here rather than as a mystery NameError inside somebody's render.
_RESOLVED_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name) for name in sorted(PACK_ALLOWED_BUILTINS)
}


def _module_allowed(name: str) -> bool:
    """Whether ``name`` is an allowed module or a submodule of one."""
    return any(name == entry or name.startswith(f"{entry}.") for entry in PACK_ALLOWED_MODULES)


def _guarded_import(
    name: str,
    # `globals`/`locals` shadow builtins because ``__import__``'s own signature
    # names them that way, and an import statement calls it positionally.
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: Sequence[str] = (),
    level: int = 0,
) -> Any:
    """``__import__`` for pack code: :data:`PACK_ALLOWED_MODULES` only.

    A relative import is refused outright — a pack is a single ``context.py``
    with no package around it, so ``level > 0`` can only be reaching for
    something the trust hash does not cover.

    The refusal names the module and nothing else, so discovery can put it
    straight into the pack's diagnosis.
    """
    if level:
        raise PackCapabilityRefused(
            f"pack code may not use a relative import ({'.' * level}{name}): a pack is one "
            f"context.py, and the trust hash covers no other file beside it"
        )
    if not _module_allowed(name):
        raise PackCapabilityRefused(
            f"pack code may not import {name!r}: a context builder turns a record into "
            f"template variables, so the pack API grants the canonical model, the pack "
            f"helpers, and pure-computation stdlib only "
            f"(see anastomosis.reconstruct.packexec.PACK_ALLOWED_MODULES)"
        )
    return importlib.__import__(name, globals, locals, fromlist, level)


def restrict_module(namespace: dict[str, Any]) -> None:
    """Install the restricted builtins into a pack module's globals, in place.

    Called on the module namespace BEFORE ``context.py`` is exec'd into it, so
    the restriction is in force for the module body AND for every later
    ``build_context`` call, which resolves its globals through this same
    mapping.

    A fresh mapping per module: two packs must not be able to reach each other
    by mutating a shared ``__builtins__``.
    """
    namespace["__builtins__"] = {**_RESOLVED_BUILTINS, "__import__": _guarded_import}
