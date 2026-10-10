"""OpenTelemetry facade: one place that creates the tracer and every metric instrument.

Providers are injected, so tests use in-memory exporters and a disabled setup is a true no-op.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, MeterProvider, NoOpMeterProvider, Observation
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NoOpTracerProvider, Span, TracerProvider

SCOPE = "settleagent"


def clean(attrs: dict[str, Any]) -> dict[str, str | bool | int | float]:
    """OTel attributes must be primitives; drop None, stringify the rest."""
    out: dict[str, str | bool | int | float] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, str | bool | int | float) else str(v)
    return out


class Telemetry:
    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._tracer_provider = tracer_provider or NoOpTracerProvider()
        self._meter_provider = meter_provider or NoOpMeterProvider()
        self.tracer = self._tracer_provider.get_tracer(SCOPE)
        meter = self._meter_provider.get_meter(SCOPE)

        self.cases_received = meter.create_counter("settleagent.cases.received")
        self.cases_processed = meter.create_counter("settleagent.cases.processed")
        self.case_duration = meter.create_histogram("settleagent.case.duration", unit="s")
        self.case_cost = meter.create_histogram("settleagent.case.cost", unit="USD")
        self.tool_calls = meter.create_counter("settleagent.tool.calls")
        self.tool_duration = meter.create_histogram("settleagent.tool.duration", unit="ms")
        self.llm_tokens = meter.create_counter("settleagent.llm.tokens", unit="token")
        self.llm_cost = meter.create_counter("settleagent.llm.cost", unit="USD")
        self.llm_duration = meter.create_histogram("settleagent.llm.duration", unit="ms")
        self.remediation_actions = meter.create_counter("settleagent.remediation.actions")
        self.budget_alerts = meter.create_counter("settleagent.budget.alerts")
        self.budget_blocked = meter.create_counter("settleagent.budget.blocked")

        self._gauges: dict[str, float] = {"queue_depth": 0.0, "pending_approvals": 0.0}
        for key, name in (
            ("queue_depth", "settleagent.queue.depth"),
            ("pending_approvals", "settleagent.approvals.pending"),
        ):
            meter.create_observable_gauge(name, callbacks=[self._gauge_callback(key)])

    def _gauge_callback(self, key: str) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def callback(_options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(self._gauges[key])]

        return callback

    def set_gauges(self, *, queue_depth: float, pending_approvals: float) -> None:
        """Updated periodically by the agent process from the database."""
        self._gauges["queue_depth"] = float(queue_depth)
        self._gauges["pending_approvals"] = float(pending_approvals)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        """Child of the current span (or a root). Exceptions are recorded and re-raised."""
        with self.tracer.start_as_current_span(name, attributes=clean(attrs)) as s:
            yield s

    @staticmethod
    def current_trace_id() -> str | None:
        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None

    def shutdown(self) -> None:
        """Flush and stop exporters (call before a short-lived process exits)."""
        for provider in (self._tracer_provider, self._meter_provider):
            stop = getattr(provider, "shutdown", None)
            if stop is not None:
                stop()


NOOP = Telemetry()


def setup_telemetry(
    service_name: str, endpoint: str, *, enabled: bool = True, metric_interval_ms: int = 10_000
) -> Telemetry:
    """Real OTLP/gRPC exporters to the collector. Export failures never raise into the app."""
    if not enabled:
        return Telemetry()
    insecure = endpoint.startswith("http://")
    resource = Resource.create({"service.name": service_name})
    tracer_provider = SdkTracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    )
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=insecure),
        export_interval_millis=metric_interval_ms,
    )
    meter_provider = SdkMeterProvider(resource=resource, metric_readers=[reader])
    return Telemetry(tracer_provider, meter_provider)


__all__ = ["NOOP", "SCOPE", "Telemetry", "clean", "metrics", "setup_telemetry"]
