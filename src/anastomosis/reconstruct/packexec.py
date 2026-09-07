"""Restricted-globals sandbox for non-built-in pack ``context.py`` (RULES.md 23).

No ``open``, ``eval``, ``exec``, ``compile``, ``input``, ``globals``, ``print``;
``__import__`` allows only :data:`PACK_ALLOWED_MODULES`. Not a security
boundary — a determined pack can still reach ``os`` via
``__class__.__subclasses__()`` — it turns silent capability into loud,
named refusal. Built-ins are exempt by origin, decided in
:mod:`anastomosis.reconstruct.packs`. PHI: a refusal names a module only.
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

    An :class:`ImportError`: :mod:`anastomosis.reconstruct.packs` diagnoses it
    like any bad pack — unavailable, named in the message, others unaffected.
    """


#: Modules a pack's ``context.py`` may import (RULES.md 23): a name matches
#: itself or any submodule. Exactly the one shipped context module, not the
#: ``anastomosis.packs`` package root — a submodule match on the root would
#: resolve ``pathlib`` right back through ``practice_fusion_soap.context.Path``.
#: The stdlib entries are pure computation only; anything absent is refused.
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

# ``time`` is allowed because ``date.strftime`` reaches for it through the
# CALLING frame's ``__import__`` (the pack's); withholding it fails every
# formatted date. Pure computation — grants nothing ``datetime`` doesn't.

#: Builtins a pack's globals carry: everything to compute over a record, none
#: that reaches past the process (RULES.md 23). ``print`` is refused because
#: stdout is where the log redaction filter cannot reach a leaked value;
#: ``eval``/``exec``/``compile`` because the trust hash covers no code they
#: would run. ``__build_class__`` stays: a ``class`` statement needs it.
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
    """Contract: ``__import__`` for pack code — :data:`PACK_ALLOWED_MODULES` only.

    Refuses a relative import (a lone ``context.py`` has no package around
    it) and any disallowed name, naming only the module in the message.
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
    """Install the restricted builtins into a pack module's globals, in place,
    before ``context.py`` execs. A fresh mapping per module: two packs must
    not be able to reach each other through a shared ``__builtins__``.
    """
    namespace["__builtins__"] = {**_RESOLVED_BUILTINS, "__import__": _guarded_import}
