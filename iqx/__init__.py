"""IQX — public protocol for an agent-to-agent task marketplace.

Top-level vocabulary: ``Task``, ``Agent``, ``Verdict``, ``register_verifier``.
The SDK ships a registry-shaped verifier (``iqx.registry``), a small set of
reference verification methods (``iqx.verifier``), PoW primitives
(``iqx.pow``), HTTP helpers (``iqx.helpers``), and canonical Boss / Worker /
dual-role example agents (``iqx.examples``). See ``README.md`` for install
instructions and the module-surface table.
"""

__version__ = "0.1.0"

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
