"""Celery worker tasks for the real workload."""

import json
import logging
import os
import time
import importlib
from datetime import datetime, timezone

from celery_app import celery_app
from tracing import extract_trace_context, get_tracer, setup_telemetry

get_task_logger = importlib.import_module("celery.utils.log").get_task_logger
CeleryInstrumentor = importlib.import_module("opentelemetry.instrumentation.celery").CeleryInstrumentor
RedisInstrumentor = importlib.import_module("opentelemetry.instrumentation.redis").RedisInstrumentor

setup_telemetry(service_name=os.getenv("OTEL_SERVICE_NAME", "real-worker"))
CeleryInstrumentor().instrument()
RedisInstrumentor().instrument()

logger = get_task_logger(__name__)
logger.setLevel(logging.INFO)
tracer = get_tracer(__name__)


def _emit(event: str, **extra) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "service": "real-worker",
    }
    payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=True))


@celery_app.task(bind=True, name="tasks.process_order")
def process_order(
    self,
    order_id: str,
    customer_id: str,
    item_count: int,
    simulate_failure: bool = False,
    trace_headers: dict = None,
):
    """Process an order asynchronously to emulate real background workload."""
    context = extract_trace_context(trace_headers)
    effective_items = max(item_count, 1)
    seconds_per_item = float(os.getenv("ORDER_PROCESSING_SECONDS_PER_ITEM", "180"))
    max_processing_seconds = float(os.getenv("ORDER_PROCESSING_MAX_SECONDS", "7200"))
    processing_seconds = min(effective_items * seconds_per_item, max_processing_seconds)

    with tracer.start_as_current_span("worker.process_order", context=context) as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.customer_id", customer_id)
        span.set_attribute("order.item_count", item_count)
        span.set_attribute("order.simulate_failure", simulate_failure)
        span.set_attribute("order.processing_seconds", processing_seconds)

        _emit(
            "order_processing_started",
            order_id=order_id,
            task_id=self.request.id,
            processing_seconds=processing_seconds,
            seconds_per_item=seconds_per_item,
        )

        # Simulate business processing and remote dependencies.
        time.sleep(processing_seconds)

        if simulate_failure:
            error_message = "Simulated worker failure for resilience testing"
            span.record_exception(RuntimeError(error_message))
            span.set_attribute("order.failed", True)
            _emit("order_processing_failed", order_id=order_id, task_id=self.request.id, reason=error_message)
            raise RuntimeError(error_message)

        result = {
            "order_id": order_id,
            "status": "processed",
            "processed_items": item_count,
            "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _emit("order_processing_completed", order_id=order_id, task_id=self.request.id)
        return result
