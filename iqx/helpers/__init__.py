"""HTTP helpers shared between the SDK reference handlers and the example
agents.

Modules:

- ``iqx.helpers.price`` — CoinGecko price helpers + WETH addresses. Single
  source of throttle / backoff state, so all consumers in the same process
  share one rate-limit budget.
- ``iqx.helpers.defillama`` — DefiLlama protocols endpoint wrapper.
"""
