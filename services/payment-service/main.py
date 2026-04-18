"""
CausalIQ Payment Service — fully instrumented with OpenTelemetry
Simulates real payment processing with DB-like latency and error conditions.
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
from opentelemetry.sdk.resources import Resource

logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"payment-service","msg":"%(message)s"}',
)
logger = logging.getLogger("payment-service")

resource = Resource.create({"service.name": "payment-service", "service.version": "1.0.0"})
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("payment-service")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True), export_interval_millis=5000
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("payment-service")

payment_counter = meter.create_counter("payments_total", description="Total payments")
error_counter   = meter.create_counter("payment_errors_total", description="Payment errors")
latency_hist    = meter.create_histogram("payment_duration_ms", description="Payment latency ms")
db_latency_hist = meter.create_histogram("payment_db_latency_ms", description="DB query latency ms")

app = FastAPI(title="CausalIQ Payment Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)

# In-memory payments ledger
PAYMENTS: dict[str, dict] = {}

# Fault injection flag — toggled by load generator
FAULT_MODE = {"active": False, "db_latency_ms": 50}


class PaymentRequest(BaseModel):
    order_id: str
    amount: float


class FaultConfig(BaseModel):
    active: bool
    db_latency_ms: int = 50


@app.get("/health")
async def health():
    return {"status": "ok", "service": "payment-service", "ts": datetime.utcnow().isoformat(), "fault_mode": FAULT_MODE}


@app.post("/admin/fault")
async def set_fault_mode(cfg: FaultConfig):
    """Real fault injection endpoint used by the load generator."""
    FAULT_MODE["active"] = cfg.active
    FAULT_MODE["db_latency_ms"] = cfg.db_latency_ms
    logger.warning("Fault mode changed: active=%s db_latency_ms=%d", cfg.active, cfg.db_latency_ms)
    return FAULT_MODE


@app.post("/payments")
async def process_payment(req: PaymentRequest):
    start = time.monotonic()
    payment_id = str(uuid.uuid4())

    with tracer.start_as_current_span("payment.process") as span:
        span.set_attribute("payment.id", payment_id)
        span.set_attribute("payment.order_id", req.order_id)
        span.set_attribute("payment.amount", req.amount)
        payment_counter.add(1)

        # Simulate DB write — possibly high latency in fault mode
        with tracer.start_as_current_span("payment.db_write") as db_span:
            base_latency = FAULT_MODE["db_latency_ms"] if FAULT_MODE["active"] else 20
            jitter = random.gauss(base_latency, base_latency * 0.3)
            db_wait = max(jitter, 5)
            await asyncio.sleep(db_wait / 1000)
            db_latency_hist.record(db_wait, {"table": "payments"})
            db_span.set_attribute("db.latency_ms", db_wait)

            # Simulate DB errors in fault mode
            if FAULT_MODE["active"] and random.random() < 0.15:
                err = Exception("DB connection pool exhausted")
                db_span.record_exception(err)
                error_counter.add(1, {"reason": "db_error"})
                logger.error("DB error order_id=%s: %s", req.order_id, str(err))
                raise HTTPException(status_code=503, detail="Database error")

        # Simulate payment gateway call
        with tracer.start_as_current_span("payment.gateway_call") as gw_span:
            gw_latency = random.gauss(80, 30)
            await asyncio.sleep(max(gw_latency, 10) / 1000)
            gw_span.set_attribute("gateway.latency_ms", gw_latency)

            # Random decline (2% baseline, 10% in fault mode)
            decline_rate = 0.10 if FAULT_MODE["active"] else 0.02
            if random.random() < decline_rate:
                error_counter.add(1, {"reason": "gateway_declined"})
                gw_span.set_attribute("gateway.result", "declined")
                raise HTTPException(status_code=402, detail="Payment declined by gateway")

        payment = {
            "payment_id": payment_id,
            "order_id": req.order_id,
            "amount": req.amount,
            "status": "success",
            "created_at": datetime.utcnow().isoformat(),
        }
        PAYMENTS[payment_id] = payment

        elapsed = (time.monotonic() - start) * 1000
        latency_hist.record(elapsed, {"endpoint": "/payments"})
        span.set_attribute("payment.elapsed_ms", elapsed)
        logger.info("Payment success payment_id=%s order_id=%s amount=%.2f latency_ms=%.2f",
                    payment_id, req.order_id, req.amount, elapsed)
        return payment


@app.get("/payments/{payment_id}")
async def get_payment(payment_id: str):
    p = PAYMENTS.get(payment_id)
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    return p

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
