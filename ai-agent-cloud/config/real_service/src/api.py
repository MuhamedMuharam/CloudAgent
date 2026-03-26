"""FastAPI application for real workload simulation."""

import json
import logging
import os
import importlib
from datetime import datetime, timezone
from uuid import uuid4

from celery_app import celery_app
from tasks import process_order
from tracing import get_tracer, inject_trace_headers, setup_telemetry

AsyncResult = importlib.import_module("celery.result").AsyncResult
FastAPI = importlib.import_module("fastapi").FastAPI
BaseModel = importlib.import_module("pydantic").BaseModel
Field = importlib.import_module("pydantic").Field
FastAPIInstrumentor = importlib.import_module("opentelemetry.instrumentation.fastapi").FastAPIInstrumentor
RedisInstrumentor = importlib.import_module("opentelemetry.instrumentation.redis").RedisInstrumentor

setup_telemetry(service_name=os.getenv("OTEL_SERVICE_NAME", "ai-real-api"))
RedisInstrumentor().instrument()

app = FastAPI(title="AI Real Service API", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

logger = logging.getLogger("ai-real-api")
logger.setLevel(logging.INFO)
tracer = get_tracer(__name__)


class CreateOrderRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    item_count: int = Field(ge=1, le=100)
    simulate_failure: bool = False


class CreateOrderResponse(BaseModel):
    order_id: str
    task_id: str
    status: str


def _emit(event: str, **extra) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "service": "ai-real-api",
    }
    payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=True))


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-real-api"}

# This endpoint simulates order creation and processing, emitting telemetry and logs for observability.
@app.post("/orders", response_model=CreateOrderResponse)
def create_order(payload: CreateOrderRequest):
    order_id = str(uuid4())

    with tracer.start_as_current_span("api.create_order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.customer_id", payload.customer_id)
        span.set_attribute("order.item_count", payload.item_count)

        trace_headers = inject_trace_headers()
        async_result = process_order.delay(
            order_id=order_id,
            customer_id=payload.customer_id,
            item_count=payload.item_count,
            simulate_failure=payload.simulate_failure,
            trace_headers=trace_headers,
        )

        _emit(
            "order_submitted",
            order_id=order_id,
            task_id=async_result.id,
            customer_id=payload.customer_id,
            item_count=payload.item_count,
            simulate_failure=payload.simulate_failure,
        )

        return {
            "order_id": order_id,
            "task_id": async_result.id,
            "status": "queued",
        }

# This endpoint allows clients to check the status of their order processing task, providing insights into the workflow.
@app.get("/tasks/{task_id}")
def get_task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)

    response = {
        "task_id": task_id,
        "state": result.state,
    }

    if result.state == "SUCCESS":
        response["result"] = result.result
    elif result.state in {"FAILURE", "REVOKED"}:
        response["error"] = str(result.result)

    return response
