"""In-memory OpenTelemetry for tests: real SDK, no network."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from settleagent.observability.telemetry import Telemetry


@dataclass
class Probe:
    telemetry: Telemetry
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader

    # ---- spans
    def spans(self, name: str | None = None) -> list[ReadableSpan]:
        spans = list(self.exporter.get_finished_spans())
        return [s for s in spans if name is None or s.name == name]

    def span(self, name: str) -> ReadableSpan:
        [s] = self.spans(name)
        return s

    def children_of(self, parent: ReadableSpan) -> list[ReadableSpan]:
        return [s for s in self.spans() if s.parent and s.parent.span_id == parent.context.span_id]

    @staticmethod
    def errored(span: ReadableSpan) -> bool:
        return span.status.status_code is StatusCode.ERROR

    # ---- metrics
    def points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        out: list[Any] = []
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == name:
                        out.extend(metric.data.data_points)
        return out

    def total(self, name: str, **attrs: Any) -> float:
        """Sum of a counter (or a histogram's recorded sum) over points matching ``attrs``."""
        total = 0.0
        for p in self.points(name):
            if all(p.attributes.get(k) == v for k, v in attrs.items()):
                total += float(p.value if hasattr(p, "value") else p.sum)
        return total

    def count(self, name: str, **attrs: Any) -> int:
        """Number of observations in a histogram over points matching ``attrs``."""
        return sum(
            p.count
            for p in self.points(name)
            if all(p.attributes.get(k) == v for k, v in attrs.items())
        )

    def attribute_sets(self, name: str) -> Iterable[dict[str, Any]]:
        return [dict(p.attributes) for p in self.points(name)]


def make_probe() -> Probe:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    return Probe(Telemetry(tracer_provider, meter_provider), exporter, reader)
