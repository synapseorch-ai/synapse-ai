"""Whether telemetry is on, and where it goes. Pure Python — no OpenTelemetry.

Nothing is exported unless the operator turns it on with a Synapse switch:

  SYNAPSE_TELEMETRY=otlp     on; the endpoint comes from OTLP_ENDPOINT, else the
                             standard OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT
  OTLP_ENDPOINT=<url>        on, to this collector (the variable Synapse has
                             always read)
  the Scale tab's saved      on, to that collector
  OTLP endpoint
  SYNAPSE_TELEMETRY=off      off, whatever else is set

The standard `OTEL_*` variables only tune an export that one of those turned
on: a machine that already has `OTEL_EXPORTER_OTLP_ENDPOINT` set for another
application does not start sending Synapse's spans there. There is no default
endpoint, not even localhost. `OTEL_SDK_DISABLED=true` wins over everything.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExportConfig:
    endpoint: str
    protocol: str  # "grpc" | "http/protobuf"
    source: str    # which switch turned it on, for the one startup line


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def sdk_disabled() -> bool:
    return _env("OTEL_SDK_DISABLED").lower() in _TRUE


def mode() -> str:
    """'otlp', 'off', or '' when SYNAPSE_TELEMETRY is unset or unrecognised."""
    value = _env("SYNAPSE_TELEMETRY").lower()
    return value if value in {"otlp", "off"} else ""


def resolve(saved_endpoint: str | None = None) -> tuple[ExportConfig | None, str | None]:
    """Decide whether to export, and to where.

    Returns (config, warning). `config` is None when telemetry is off; `warning`
    is a line worth printing once, when the operator asked for telemetry and
    will not get it.
    """
    if sdk_disabled() or mode() == "off":
        return None, None

    endpoint, source = _env("OTLP_ENDPOINT"), "OTLP_ENDPOINT"
    if not endpoint and saved_endpoint:
        endpoint, source = saved_endpoint.strip(), "the saved OTLP endpoint"
    if not endpoint and mode() == "otlp":
        for name in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            if _env(name):
                endpoint, source = _env(name), f"SYNAPSE_TELEMETRY=otlp ({name})"
                break
    if not endpoint:
        if mode() == "otlp":
            return None, ("[telemetry] SYNAPSE_TELEMETRY=otlp is set but no collector is: set "
                          "OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT. Telemetry stays off.")
        return None, None

    protocol = (_env("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or _env("OTEL_EXPORTER_OTLP_PROTOCOL")
                or "grpc").lower()
    if protocol not in {"grpc", "http/protobuf"}:
        protocol = "grpc"
    return ExportConfig(endpoint=endpoint, protocol=protocol, source=source), None


def metrics_pull_enabled() -> bool:
    """`/api/v2/metrics` serves only when METRICS_TOKEN is set. Nothing is sent."""
    return bool(_env("METRICS_TOKEN")) and not sdk_disabled()
