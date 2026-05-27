"""DefiLlama protocols endpoint wrapper.

A thin shared helper so the verifier and other consumers don't each
re-implement the protocols-list fetch.
"""

from __future__ import annotations

import requests

# Local timeout constant. Independent of ``iqx.helpers.price.HTTP_TIMEOUT_SEC``
# — a future change to one provider's timeout shouldn't accidentally retune another.
HTTP_TIMEOUT_SEC = 20

DEFILLAMA_URL = "https://api.llama.fi/protocols"


def fetch_protocols() -> list[dict]:
    """Return the full DefiLlama protocols list, raising on any non-200."""
    resp = requests.get(DEFILLAMA_URL, timeout=HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()
