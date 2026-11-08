"""What one turn's model calls used, summed — for a caller that reports it (OAI1).

The OpenAI-compatible endpoint answers with `usage`, as OpenAI does, and a turn
is several model calls (each tool round is one). `start()` puts a meter on the
caller's context; every model call made under it, in any task it starts, adds
its tokens (`core.llm_providers.generate_response_ex`). Outside a meter, `add`
does nothing.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class Meter:
    input_tokens: int = 0          # what no cache served
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    _token: Token | None = field(default=None, repr=False)

    def totals(self) -> dict:
        """In OpenAI's terms: `prompt_tokens` is all of the input, cached or not."""
        return {
            "prompt_tokens": self.input_tokens + self.cache_read_tokens + self.cache_write_tokens,
            "completion_tokens": self.output_tokens,
            "cached_tokens": self.cache_read_tokens,
            "calls": self.calls,
        }


_meter: ContextVar[Meter | None] = ContextVar("turn_usage_meter", default=None)


def start() -> Meter:
    meter = Meter()
    meter._token = _meter.set(meter)
    return meter


def stop(meter: Meter) -> None:
    if meter._token is not None:
        try:
            _meter.reset(meter._token)
        except ValueError:
            _meter.set(None)   # stopped from another context: just detach
        meter._token = None


def add(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0) -> None:
    """Count one model call's tokens toward the turn's meter, if there is one. Never raises."""
    meter = _meter.get()
    if meter is None:
        return
    try:
        meter.input_tokens += int(input_tokens or 0)
        meter.output_tokens += int(output_tokens or 0)
        meter.cache_read_tokens += int(cache_read_tokens or 0)
        meter.cache_write_tokens += int(cache_write_tokens or 0)
        meter.calls += 1
    except (TypeError, ValueError):
        pass
