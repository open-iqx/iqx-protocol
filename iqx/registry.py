"""Verifier registry — the SDK interface for ``@register_verifier``.

External Worker authors writing a new verification method import from this
module rather than ``iqx.verifier`` so they do not transitively pull in the
reference handlers and their network-helper deps (``requests``, the
CoinGecko / DefiLlama path through ``iqx.helpers.price``). Module-level
dependency is ``iqx.schema`` only — ``sqlmodel`` is pulled in transitively
via ``iqx.schema`` because that's where ``Task`` lives. The reference
handlers themselves live in ``iqx.verifier`` and register against
``_REGISTRY`` at import time via the decorator below.
"""

from __future__ import annotations

from typing import Callable

from iqx.schema import Task

# ---- public types ------------------------------------------------------------

#: Return shape of every verification method: ``(verified, notes)``.
#:
#: Kept as a plain tuple alias rather than a NamedTuple in this PR so handler
#: bodies and signatures move byte-identical from the pre-Phase-B-Step-3
#: ``verifier.py`` (handlers keep ``-> tuple[bool, str]`` literally). The
#: ``Verdict`` name is the SDK-public type vocabulary, used only by the new
#: ``VerifyFn`` alias and the ``verify()`` dispatcher introduced in this PR.
Verdict = tuple[bool, str]


# ---- registry ----------------------------------------------------------------

# A verification handler is `(task, context) -> (verified, notes)`. Context is
# a free-form dict each method's loader populates (e.g. the TVL method needs
# the DefiLlama protocol index; the echo method needs nothing).
VerifyFn = Callable[[Task, dict], tuple[bool, str]]
_REGISTRY: dict[str, VerifyFn] = {}


def register_verifier(method_id: str) -> Callable[[VerifyFn], VerifyFn]:
    def decorator(fn: VerifyFn) -> VerifyFn:
        _REGISTRY[method_id] = fn
        return fn
    return decorator


def verify(task: Task, ctx: dict) -> Verdict:
    """Dispatch a task to its registered handler and return the Verdict.

    Resolves the handler via ``task.verification_method``. Unknown methods
    return a graceful FAIL Verdict — they are NOT raised, so an external
    poller can decide whether to skip / log / defer.
    """
    method = task.verification_method
    handler = _REGISTRY.get(method)
    if handler is None:
        return False, f"unknown verification_method: {method!r}"
    return handler(task, ctx)
