"""
CausalIQ Order Service — fully instrumented with OpenTelemetry
"""
import os
import time
import uuid
import random
import logging
import asyncio
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"order-service","msg":"%(message)s"}',
)
logger = logging.getLogger("order-service")

resource = Resource.create({"service.name": "order-service", "service.version": "1.0.0"})
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("order-service")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True), export_interval_millis=5000
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("order-service")

order_counter   = meter.create_counter("orders_total", description="Total orders")
error_counter   = meter.create_counter("order_errors_total", description="Order errors")
latency_hist    = meter.create_histogram("order_request_duration_ms", description="Order latency")
order_value_hist = meter.create_histogram("order_value_usd", description="Order value USD")

app = FastAPI(title="CausalIQ Order Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

PAYMENT_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8002")
AUTH_URL    = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8000")

# In-memory order store (real state, not mock)
ORDERS: dict[str, dict] = {}

class OrderRequest(BaseModel):
    product_id: str
    quantity:   int
    amount:     float

@app.get("/health")
async def health():
    return {"status": "ok", "service": "order-service", "ts": datetime.utcnow().isoformat()}

@app.get("/orders")
async def list_orders():
    return {"orders": list(ORDERS.values()), "count": len(ORDERS)}

@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    order = ORDERS.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.post("/orders")
async def create_order(req: OrderRequest, authorization: str = Header(default="")):
    start = time.monotonic()
    order_id = str(uuid.uuid4())

    with tracer.start_as_current_span("order.create") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.product_id", req.product_id)
        span.set_attribute("order.quantity", req.quantity)
        span.set_attribute("order.amount", req.amount)
        order_counter.add(1, {"product": req.product_id})
        order_value_hist.record(req.amount)

        # Validate token with auth service
        with tracer.start_as_current_span("order.validate_auth") as auth_span:
            try:
                async with httpx.AsyncClient() as client:
                    auth_resp = await client.get(
                        f"{AUTH_URL}/auth/validate",
                        headers={"Authorization": authorization},
                        timeout=3,
                    )
                if auth_resp.status_code != 200:
                    error_counter.add(1, {"reason": "auth_failed"})
                    raise HTTPException(status_code=401, detail="Auth validation failed")
                auth_data = auth_resp.json()
                auth_span.set_attribute("user.name", auth_data.get("user", ""))
            except httpx.TimeoutException:
                auth_span.set_attribute("error", "auth_timeout")
                error_counter.add(1, {"reason": "auth_timeout"})
                raise HTTPException(status_code=503, detail="Auth service timeout")

        # Simulate DB write latency
        db_latency = random.gauss(30, 10)
        await asyncio.sleep(max(db_latency, 5) / 1000)

        # Call payment service
        with tracer.start_as_current_span("order.call_payment") as pay_span:
            try:
                async with httpx.AsyncClient() as client:
                    pay_resp = await client.post(
                        f"{PAYMENT_URL}/payments",
                        json={"order_id": order_id, "amount": req.amount},
                        headers={"Authorization": authorization},
                        timeout=5,
                    )
                pay_span.set_attribute("payment.status", pay_resp.status_code)
                payment_data = pay_resp.json()
            except httpx.TimeoutException as exc:
                pay_span.record_exception(exc)
                error_counter.add(1, {"reason": "payment_timeout"})
                raise HTTPException(status_code=503, detail="Payment service timeout")

        if pay_resp.status_code != 200:
            error_counter.add(1, {"reason": "payment_failed"})
            raise HTTPException(status_code=402, detail="Payment failed")

        order = {
            "id": order_id,
            "product_id": req.product_id,
            "quantity": req.quantity,
            "amount": req.amount,
            "status": "confirmed",
            "payment_id": payment_data.get("payment_id"),
            "created_at": datetime.utcnow().isoformat(),
        }
        ORDERS[order_id] = order

        elapsed = (time.monotonic() - start) * 1000
        latency_hist.record(elapsed, {"endpoint": "/orders"})
        span.set_attribute("order.elapsed_ms", elapsed)
        logger.info("Order created order_id=%s amount=%.2f latency_ms=%.2f", order_id, req.amount, elapsed)
        return order

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
