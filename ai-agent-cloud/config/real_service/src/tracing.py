"""
Telemetry utilities for FastAPI + Celery services.
Configures OpenTelemetry with OTLP export (intended for local ADOT collector).
"""

import os
import importlib
from typing import Dict, Optional

_TELEMETRY_READY = False


def _load_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def setup_telemetry(service_name: str, service_version: str = "1.0.0") -> None:
    """Initialize OpenTelemetry once per process."""
    global _TELEMETRY_READY
    if _TELEMETRY_READY:
        return

    Resource = _load_attr("opentelemetry.sdk.resources", "Resource")
    TracerProvider = _load_attr("opentelemetry.sdk.trace", "TracerProvider")
    BatchSpanProcessor = _load_attr("opentelemetry.sdk.trace.export", "BatchSpanProcessor")
    OTLPSpanExporter = _load_attr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "OTLPSpanExporter",
    )
    trace = importlib.import_module("opentelemetry.trace")
    propagate = importlib.import_module("opentelemetry.propagate")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": os.getenv("APP_ENV", "dev"),
        }
    )

    tracer_provider_kwargs = {"resource": resource}

    try:
        AwsXRayIdGenerator = _load_attr(
            "opentelemetry.sdk.extension.aws.trace",
            "AwsXRayIdGenerator",
        )
        tracer_provider_kwargs["id_generator"] = AwsXRayIdGenerator()
    except Exception:
        pass

    tracer_provider = TracerProvider(**tracer_provider_kwargs)
    trace.set_tracer_provider(tracer_provider)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    exporter = OTLPSpanExporter(endpoint=endpoint, timeout=10)
    tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    try:
        AwsXRayPropagator = _load_attr(
            "opentelemetry.propagators.aws",
            "AwsXRayPropagator",
        )
        propagate.set_global_textmap(AwsXRayPropagator())
    except Exception:
        pass

    _TELEMETRY_READY = True


def get_tracer(name: str):
    """Get tracer for the current provider."""
    trace = importlib.import_module("opentelemetry.trace")
    return trace.get_tracer(name)


def inject_trace_headers() -> Dict[str, str]:
    """Inject current trace context into a serializable carrier."""
    propagate = importlib.import_module("opentelemetry.propagate")
    carrier: Dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: Optional[Dict[str, str]]):
    """Extract trace context from a serialized carrier."""
    propagate = importlib.import_module("opentelemetry.propagate")
    return propagate.extract(carrier or {})
