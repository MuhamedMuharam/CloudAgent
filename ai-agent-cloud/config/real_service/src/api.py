"""FastAPI application for real workload simulation."""

import json
import logging
import os
import importlib
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional

from celery_app import celery_app
from tasks import process_order
from tracing import get_tracer, inject_trace_headers, setup_telemetry

AsyncResult = importlib.import_module("celery.result").AsyncResult
FastAPI = importlib.import_module("fastapi").FastAPI
HTTPException = importlib.import_module("fastapi").HTTPException
BaseModel = importlib.import_module("pydantic").BaseModel
Field = importlib.import_module("pydantic").Field
FastAPIInstrumentor = importlib.import_module("opentelemetry.instrumentation.fastapi").FastAPIInstrumentor
RedisInstrumentor = importlib.import_module("opentelemetry.instrumentation.redis").RedisInstrumentor
RedisClient = importlib.import_module("redis").Redis

setup_telemetry(service_name=os.getenv("OTEL_SERVICE_NAME", "real-api"))
RedisInstrumentor().instrument()

app = FastAPI(title="Real Service API", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

logger = logging.getLogger("real-api")
logger.setLevel(logging.INFO)
tracer = get_tracer(__name__)

ORDER_IDS_KEY = "real-service:orders:index"
ORDER_KEY_PREFIX = "real-service:order:"
TASK_TO_ORDER_KEY = "real-service:tasks:order"
MAX_STORED_ORDERS = 2000
_redis_client: Optional[object] = None


def _get_redis_client() -> Optional[object]:
    """Return Redis client for order index storage, if configured."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    redis_url = os.getenv("CELERY_RESULT_BACKEND") or os.getenv("CELERY_BROKER_URL")
    if not redis_url or not redis_url.startswith(("redis://", "rediss://")):
        return None

    try:
        _redis_client = RedisClient.from_url(redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        _redis_client = None
        return None


def _save_order_index(order_id: str, task_id: str, payload: object) -> None:
    redis_client = _get_redis_client()
    if redis_client is None:
        return

    order_data = {
        "order_id": order_id,
        "task_id": task_id,
        "customer_id": payload.customer_id,
        "item_count": payload.item_count,
        "simulate_failure": payload.simulate_failure,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    order_key = f"{ORDER_KEY_PREFIX}{order_id}"
    pipe = redis_client.pipeline()
    pipe.set(order_key, json.dumps(order_data, ensure_ascii=True))
    pipe.lpush(ORDER_IDS_KEY, order_id)
    pipe.ltrim(ORDER_IDS_KEY, 0, MAX_STORED_ORDERS - 1)
    pipe.hset(TASK_TO_ORDER_KEY, task_id, order_id)
    pipe.execute()


def _known_task_id(task_id: str) -> Optional[bool]:
    redis_client = _get_redis_client()
    if redis_client is None:
        return None
    return bool(redis_client.hexists(TASK_TO_ORDER_KEY, task_id))


def _order_id_for_task(task_id: str) -> Optional[str]:
    redis_client = _get_redis_client()
    if redis_client is None:
        return None
    return redis_client.hget(TASK_TO_ORDER_KEY, task_id)


def _list_orders_from_index(limit: int) -> list:
    redis_client = _get_redis_client()
    if redis_client is None:
        return []

    order_ids = redis_client.lrange(ORDER_IDS_KEY, 0, limit - 1)
    orders = []
    for order_id in order_ids:
        order_key = f"{ORDER_KEY_PREFIX}{order_id}"
        serialized = redis_client.get(order_key)
        if not serialized:
            continue
        try:
            orders.append(json.loads(serialized))
        except Exception:
            continue
    return orders


def _get_order_from_index(order_id: str) -> Optional[dict]:
    redis_client = _get_redis_client()
    if redis_client is None:
        return None

    serialized = redis_client.get(f"{ORDER_KEY_PREFIX}{order_id}")
    if not serialized:
        return None

    try:
        return json.loads(serialized)
    except Exception:
        return None


def _persist_order(order: dict) -> None:
    redis_client = _get_redis_client()
    if redis_client is None:
        return

    order_id = order.get("order_id")
    if not order_id:
        return

    redis_client.set(f"{ORDER_KEY_PREFIX}{order_id}", json.dumps(order, ensure_ascii=True))


def _enrich_order_with_task_state(order: dict) -> dict:
    enriched = dict(order)
    task_id = enriched.get("task_id")
    if not task_id:
        return enriched

    task = AsyncResult(task_id, app=celery_app)
    enriched["state"] = task.state

    if task.state == "SUCCESS":
        enriched["result"] = task.result
    elif task.state in {"FAILURE", "REVOKED"}:
        enriched["error"] = str(task.result)

    return enriched


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
        "service": "real-api",
    }
    payload.update(extra)
    logger.info(json.dumps(payload, ensure_ascii=True))


@app.get("/health")
def health():
    return {"status": "ok", "service": "real-api"}

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

        _save_order_index(order_id=order_id, task_id=async_result.id, payload=payload)

        return {
            "order_id": order_id,
            "task_id": async_result.id,
            "status": "queued",
        }


@app.get("/orders")
def list_orders(limit: int = 100):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")

    orders = _list_orders_from_index(limit=limit)
    enriched_orders = []

    for order in orders:
        enriched_orders.append(_enrich_order_with_task_state(order))

    return {
        "count": len(enriched_orders),
        "orders": enriched_orders,
    }


@app.get("/orders/stats")
def get_order_stats(limit: int = 500):
    if limit < 1 or limit > 2000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 2000")

    orders = _list_orders_from_index(limit=limit)
    enriched_orders = [_enrich_order_with_task_state(order) for order in orders]
    state_counter = Counter(order.get("state", "UNKNOWN") for order in enriched_orders)

    return {
        "sample_size": len(enriched_orders),
        "state_counts": dict(state_counter),
        "failed_orders": sum(1 for order in enriched_orders if order.get("state") in {"FAILURE", "REVOKED"}),
        "successful_orders": sum(1 for order in enriched_orders if order.get("state") == "SUCCESS"),
        "queued_or_running_orders": sum(1 for order in enriched_orders if order.get("state") in {"PENDING", "RECEIVED", "STARTED", "RETRY"}),
    }


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = _get_order_from_index(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Unknown order_id: {order_id}")

    return _enrich_order_with_task_state(order)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, terminate: bool = False):
    order = _get_order_from_index(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Unknown order_id: {order_id}")

    task_id = order.get("task_id")
    if not task_id:
        raise HTTPException(status_code=409, detail=f"Order {order_id} has no associated task_id")

    result = AsyncResult(task_id, app=celery_app)
    if result.state in {"SUCCESS", "FAILURE", "REVOKED"}:
        return {
            "order_id": order_id,
            "task_id": task_id,
            "state": result.state,
            "cancel_requested": False,
            "message": "Task already finished; no cancel action taken.",
        }

    celery_app.control.revoke(task_id, terminate=terminate)

    order["cancel_requested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    order["cancel_requested"] = True
    _persist_order(order)

    _emit(
        "order_cancel_requested",
        order_id=order_id,
        task_id=task_id,
        terminate=terminate,
        previous_state=result.state,
    )

    return {
        "order_id": order_id,
        "task_id": task_id,
        "cancel_requested": True,
        "terminate": terminate,
        "state_at_request_time": result.state,
    }

# This endpoint allows clients to check the status of their order processing task, providing insights into the workflow.
@app.get("/tasks/{task_id}")
def get_task_status(task_id: str):
    known_task = _known_task_id(task_id)
    result = AsyncResult(task_id, app=celery_app)

    if known_task is False and result.state == "PENDING":
        raise HTTPException(
            status_code=404,
            detail=f"Unknown task_id: {task_id}",
        )

    response = {
        "task_id": task_id,
        "state": result.state,
    }

    order_id = _order_id_for_task(task_id)
    if order_id:
        response["order_id"] = order_id

    if result.state == "SUCCESS":
        response["result"] = result.result
    elif result.state in {"FAILURE", "REVOKED"}:
        response["error"] = str(result.result)

    return response
