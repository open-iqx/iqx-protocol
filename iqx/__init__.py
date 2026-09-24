"""IQX — reference SDK for the IQX protocol for forward-only agent evaluation.

Top-level vocabulary: ``Task``, ``Agent``, ``Verdict``, ``register_verifier``.
The SDK ships a registry-shaped verifier (``iqx.registry``), a small set of
reference verification methods (``iqx.verifier``), PoW primitives
(``iqx.pow``), HTTP helpers (``iqx.helpers``), Boss / Worker / dual-role
example agents (``iqx.examples``), and a local development node
(``iqx.local``). See ``README.md`` for install instructions and the
module-surface table, and ``QUICKSTART.md`` for one local round.
"""

__version__ = "0.1.1"

# Top-level re-exports — the SDK vocabulary external developers reach for.
#
# Source modules are intentionally split so ``import iqx`` does NOT
# transitively load ``iqx.verifier`` (or its reference-handler deps:
# ``requests``, the CoinGecko / DefiLlama path through
# ``iqx.helpers.price``). ``iqx.schema`` is still loaded because ``Task``
# and ``Agent`` come from it, and ``iqx.schema`` itself imports
# ``sqlmodel`` — so ``sqlmodel`` is part of the baseline ``import iqx``
# footprint. The meaningful claim is: external Worker authors writing a
# new verification method can ``from iqx import register_verifier`` (or
# ``from iqx.registry import …``) without pulling in the reference
# handlers and their network-helper deps. Callers that need the
# reference handlers explicitly do ``import iqx.verifier`` themselves.
from iqx.schema import Agent, Task
from iqx.registry import Verdict, register_verifier

__all__ = [
    "__version__",
    "Agent",
    "Task",
    "Verdict",
    "register_verifier",
]
