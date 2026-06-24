import logging
import os
from typing import Any, Callable, TypeVar, cast
from functools import wraps

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter

logger = logging.getLogger(__name__)

# Try importing OTLP exporters if available
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    has_otlp = True
except ImportError:
    has_otlp = False

# Setup trace provider
provider = TracerProvider()
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("graphify-sidecar")

# Configure trace exporter
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if has_otlp and otlp_endpoint:
    logger.info(f"Configuring OTLP span exporter to endpoint: {otlp_endpoint}")
    span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
else:
    logger.info("OTLP Span Exporter not configured. Falling back to Console Span Exporter.")
    span_processor = BatchSpanProcessor(ConsoleSpanExporter())

provider.add_span_processor(span_processor)

# Setup metrics provider
metric_readers = []
if has_otlp and otlp_endpoint:
    logger.info(f"Configuring OTLP metrics exporter to endpoint: {otlp_endpoint}")
    metric_readers.append(
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=otlp_endpoint))
    )
else:
    logger.info("OTLP Metric Exporter not configured. Falling back to Console Metric Exporter.")
    metric_readers.append(
        PeriodicExportingMetricReader(ConsoleMetricExporter())
    )

metrics.set_meter_provider(MeterProvider(metric_readers=metric_readers))
meter = metrics.get_meter("graphify-sidecar")

# Common telemetry metrics
request_counter = meter.create_counter(
    name="sidecar_requests_total",
    description="Total count of incoming sidecar requests",
    unit="1"
)

F = TypeVar("F", bound=Callable[..., Any])

def trace_async(name: str) -> Callable[[F], F]:
    """Decorator to trace an asynchronous function."""
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request_counter.add(1, {"endpoint": name})
            with tracer.start_as_current_span(name) as span:
                span.set_attribute("function.name", func.__name__)
                
                # Record arguments as span attributes (skipping object self/cls pointers)
                for i, arg in enumerate(args):
                    if i == 0 and hasattr(arg, "__class__") and not isinstance(arg, (str, int, float, bool)):
                        continue
                    val_str = str(arg)
                    if len(val_str) > 1000:
                        val_str = val_str[:1000] + "..."
                    span.set_attribute(f"function.args.{i}", val_str)
                    
                for k, v in kwargs.items():
                    val_str = str(v)
                    if len(val_str) > 1000:
                        val_str = val_str[:1000] + "..."
                    span.set_attribute(f"function.kwargs.{k}", val_str)

                try:
                    result = await func(*args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise
        return cast(F, wrapper)
    return decorator

