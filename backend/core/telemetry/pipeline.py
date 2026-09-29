"""Turn OpenTelemetry on for this process — only when an operator asked.

`setup()` is called once by each process entry point (the API's lifespan, the
worker's startup, an embedder's own processes). It reads the switch
(`config.py`); when nothing turned telemetry on it returns False having
installed nothing — no tracer provider, no meter provider, no instrumentation.

When it is on:
* a tracer provider whose spans pass the content guard (`guard.py`) before an
  OTLP exporter (gRPC by default, http/protobuf when OTEL_EXPORTER_OTLP_PROTOCOL
  says so), sampled by `DropParentlessClient` around the standard sampler;
* the W3C `traceparent` propagator only — no baggage, which would travel to
  every outbound host;
* httpx and Redis instrumentation, and SQLAlchemy per engine
  (`instrument_engine`), with the SQL commenter off;
* a meter provider whose views keep only reviewed instruments and low-cardinality
  attributes; it exports over OTLP, and also feeds `/api/v2/metrics` when
  METRICS_TOKEN is set.

A missing package is one line saying what to install, never a crash.
"""
from __future__ import annotations

import logging
import os
import threading
import weakref

from core.telemetry import config, state
from core.telemetry.attributes import METRIC_ATTRIBUTES

logger = logging.getLogger("core.telemetry")

_lock = threading.Lock()
_engines: "weakref.WeakSet" = weakref.WeakSet()
_warned: set[str] = set()

INSTALL_HINT = ("pip install 'synapse-orch-ai[telemetry]' "
                "(or: pip install -r backend/requirements-telemetry.txt)")


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def setup(service_name: str, *, saved_endpoint: str | None = None, release: str | None = None,
          span_exporter=None, metric_reader=None) -> bool:
    """Install telemetry for this process if it is switched on. Idempotent.

    `span_exporter` / `metric_reader` replace the OTLP ones (tests, and an
    embedder with its own pipeline); passing one counts as turning it on.
    Returns whether spans are being exported.
    """
    with _lock:
        if state.configured:
            return state.tracing
        cfg, warning = config.resolve(saved_endpoint)
        if warning:
            _warn_once("no-endpoint", warning)
        pull = config.metrics_pull_enabled()
        if cfg is None and span_exporter is None and not pull:
            state.configured = True
            _pending_engines.clear()
            return False
        try:
            _install(service_name, cfg, pull, release, span_exporter, metric_reader)
        except ImportError as exc:
            _warn_once("missing", f"[telemetry] turned on, but the OpenTelemetry packages are "
                                  f"not installed ({exc.name}): {INSTALL_HINT}. Telemetry stays off.")
            return False
        except Exception as exc:  # never take the process down for telemetry
            _warn_once("failed", f"[telemetry] setup failed ({type(exc).__name__}); telemetry stays off.")
            return False
        return state.tracing


def _install(service_name, cfg, pull, release, span_exporter, metric_reader) -> None:
    # The stable HTTP conventions name things without a raw path; the GenAI
    # extension of the botocore instrumentation (Cloud's gateway) must never
    # capture message content. Both are read once, on first instrumentation.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")

    from opentelemetry import metrics, propagate, trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    attributes = {"service.name": os.getenv("OTEL_SERVICE_NAME") or service_name}
    version = release or os.getenv("SYNAPSE_RELEASE")
    if version:
        attributes["service.version"] = version
    resource = Resource.create(attributes)

    tracing = cfg is not None or span_exporter is not None
    if tracing:
        existing = trace.get_tracer_provider()
        if not isinstance(existing, trace.ProxyTracerProvider):
            _warn_once("provider", "[telemetry] another library installed a tracer provider first "
                                   f"({type(existing).__name__}); Synapse's spans go there.")
        else:
            trace.set_tracer_provider(_tracer_provider(resource, cfg, span_exporter))
        propagate.set_global_textmap(TraceContextTextMapPropagator())
        _instrument_libraries()

    readers = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif cfg is not None:
        readers.append(_otlp_metric_reader(cfg))
    if pull:
        try:
            from opentelemetry.exporter.prometheus import PrometheusMetricReader
            readers.append(PrometheusMetricReader())
        except ImportError as exc:
            _warn_once("prometheus", f"[telemetry] METRICS_TOKEN is set but the Prometheus exporter "
                                     f"is not installed ({exc.name}): {INSTALL_HINT}.")
    if readers:
        from opentelemetry.sdk.metrics import MeterProvider
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers,
                                                 views=_views()))
    state.tracing = tracing
    state.metering = bool(readers)
    if tracing:
        where = cfg.endpoint if cfg else "a custom exporter"
        via = cfg.source if cfg else "setup()"
        logger.info("[telemetry] OpenTelemetry on (%s) → %s. Nothing but ids, timings, counts "
                    "and cost is exported.", via, where)
    state.configured = True
    for engine in list(_pending_engines):
        instrument_engine(engine)
    _pending_engines.clear()


def _tracer_provider(resource, cfg, span_exporter):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from core.telemetry.guard import ContentGuardExporter
    from core.telemetry.sampling import sampler

    provider = TracerProvider(resource=resource, sampler=sampler())
    exporter = span_exporter if span_exporter is not None else _otlp_span_exporter(cfg)
    provider.add_span_processor(BatchSpanProcessor(ContentGuardExporter(exporter)))
    return provider


def _otlp_url(cfg, signal: str) -> str:
    base = cfg.endpoint.rstrip("/")
    return base if base.endswith(f"/v1/{signal}") else f"{base}/v1/{signal}"


def _otlp_span_exporter(cfg):
    if cfg.protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter(endpoint=_otlp_url(cfg, "traces"))
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    kwargs = {"endpoint": cfg.endpoint}
    if cfg.endpoint.startswith("http://"):
        kwargs["insecure"] = True
    return OTLPSpanExporter(**kwargs)


def _otlp_metric_reader(cfg):
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    if cfg.protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        exporter = OTLPMetricExporter(endpoint=_otlp_url(cfg, "metrics"))
    else:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        kwargs = {"endpoint": cfg.endpoint}
        if cfg.endpoint.startswith("http://"):
            kwargs["insecure"] = True
        exporter = OTLPMetricExporter(**kwargs)
    interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL") or 60000)
    return PeriodicExportingMetricReader(exporter, export_interval_millis=interval)


#: Instruments kept, by name; every other instrument is dropped. An
#: instrumentation library's metric (an outbound call's duration by host, a
#: connection pool's usage) is not exported until it is reviewed and added.
KEPT_INSTRUMENTS = ("synapse.*", "http.server.request.duration")


def _views():
    from opentelemetry.sdk.metrics.view import DropAggregation, View
    views = [View(instrument_name=name, attribute_keys=set(METRIC_ATTRIBUTES))
             for name in KEPT_INSTRUMENTS]
    views.append(View(instrument_name="*", aggregation=DropAggregation()))
    return views


def _instrument_libraries() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except ImportError as exc:
        _warn_once("httpx", f"[telemetry] httpx calls are not traced ({exc.name} missing): {INSTALL_HINT}.")
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    except ImportError as exc:
        _warn_once("redis", f"[telemetry] Redis calls are not traced ({exc.name} missing): {INSTALL_HINT}.")


#: Engines built before `setup()` decided; weak, so an install that never
#: turns telemetry on holds none of them alive.
_pending_engines: "weakref.WeakSet" = weakref.WeakSet()


def instrument_engine(engine) -> None:
    """Trace one SQLAlchemy engine's statements (never their text: the guard
    keeps the operation only). Safe to call before setup and when off: an
    engine built before setup is instrumented when setup turns tracing on."""
    sync_engine = getattr(engine, "sync_engine", engine)
    if not state.configured:
        _pending_engines.add(sync_engine)
        return
    if not state.tracing or sync_engine in _engines:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
        from opentelemetry.instrumentation.sqlalchemy.version import __version__
    except ImportError as exc:
        _warn_once("sqlalchemy", f"[telemetry] database calls are not traced ({exc.name} missing): "
                                 f"{INSTALL_HINT}.")
        return
    tracer = trace.get_tracer("opentelemetry.instrumentation.sqlalchemy", __version__)
    usage = metrics.get_meter("opentelemetry.instrumentation.sqlalchemy", __version__) \
        .create_up_down_counter("db.client.connections.usage", unit="connections")
    EngineTracer(tracer, sync_engine, usage, enable_commenter=False)
    _engines.add(sync_engine)


def reset_for_tests() -> None:
    """Forget this process's providers so a test can install its own."""
    from opentelemetry import metrics, trace
    from opentelemetry.util._once import Once

    with _lock:
        state.configured = False
        state.tracing = False
        state.metering = False
        _warned.clear()
        _pending_engines.clear()
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace._TRACER_PROVIDER = None
        metrics._internal._METER_PROVIDER_SET_ONCE = Once()
        metrics._internal._METER_PROVIDER = None
