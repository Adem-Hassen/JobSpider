"""AI spend accounting: cost per call from usage and the configured price table."""

from __future__ import annotations

from dataclasses import dataclass

from jobfinder.ai.client import ChatResult, TokenUsage
from jobfinder.config import AIProvider, Price, ProvidersFile
from jobfinder.storage.state_store import SpendEntry, StateStore

_PER_MILLION = 1_000_000


def cost_usd(price: Price, usage: TokenUsage) -> float:
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    return (
        uncached * price.input
        + usage.cached_input_tokens * price.cached_input
        + usage.output_tokens * price.output
    ) / _PER_MILLION


@dataclass(frozen=True)
class SpendLedger:
    """Records every AI call's tokens and cost in state.db (budget caps arrive in M4)."""

    store: StateStore
    providers: ProvidersFile

    def cost_of(self, result: ChatResult) -> float:
        provider = self.providers.providers[AIProvider(result.provider)]
        return cost_usd(provider.price_at(result.model, result.requested_at), result.usage)

    def record(self, result: ChatResult, purpose: str) -> float:
        cost = self.cost_of(result)
        self.store.record_spend(
            SpendEntry(
                at=result.requested_at,
                provider=result.provider,
                model=result.model,
                purpose=purpose,
                input_tokens=result.usage.input_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=cost,
            )
        )
        return cost
