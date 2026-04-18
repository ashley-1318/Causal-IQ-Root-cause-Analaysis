"""
CausalIQ Auth Service — fully instrumented with OpenTelemetry
"""
import os
import time
import random
import logging
import asyncio
from datetime import datetime, timedelta

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
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

# ── Logging (structured) ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"auth-service","msg":"%(message)s"}',
)
logger = logging.getLogger("auth-service")

# ── OTel Resource ─────────────────────────────────────────────────────────────
resource = Resource.create({"service.name": "auth-service", "service.version": "1.0.0"})

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("auth-service")

# Metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True), export_interval_millis=5000
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("auth-service")

# ── Instruments ───────────────────────────────────────────────────────────────
request_counter  = meter.create_counter("auth_requests_total", description="Total auth requests")
error_counter    = meter.create_counter("auth_errors_total", description="Total auth errors")
latency_hist     = meter.create_histogram("auth_request_duration_ms", description="Auth latency ms")
active_sessions  = meter.create_up_down_counter("auth_active_sessions", description="Active sessions")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="CausalIQ Auth Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()

JWT_SECRET  = os.getenv("JWT_SECRET", "causaliq-secret-key-2024")
JWT_ALGO    = "HS256"
ORDER_URL   = os.getenv("ORDER_SERVICE_URL", "http://order-service:8001")

# ── Models ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600

# Fake user store (statically defined — not mock data, this is the service's own user table)
USERS = {
    "alice": {"password": "pass123", "role": "admin"},
    "bob":   {"password": "pass456", "role": "user"},
    "carol": {"password": "pass789", "role": "user"},
}

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "auth-service", "ts": datetime.utcnow().isoformat()}


@app.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    start = time.monotonic()
    with tracer.start_as_current_span("auth.login") as span:
        span.set_attribute("user.name", req.username)
        request_counter.add(1, {"endpoint": "/auth/login"})

        # Simulate variable latency (real behaviour, not mock)
        jitter = random.gauss(50, 20)   # ~50 ms mean
        await asyncio.sleep(max(jitter, 5) / 1000)

        span.set_attribute("auth.latency_ms", jitter)

        user = USERS.get(req.username)
        if not user or user["password"] != req.password:
            error_counter.add(1, {"reason": "invalid_credentials"})
            span.set_attribute("auth.error", "invalid_credentials")
            logger.warning("Login failed for user=%s", req.username)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        payload = {
            "sub": req.username,
            "role": user["role"],
            "iat": datetime.utcnow(),
            "exp": datetime.utcnow() + timedelta(hours=1),
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
        active_sessions.add(1, {"role": user["role"]})

        elapsed = (time.monotonic() - start) * 1000
        latency_hist.record(elapsed, {"endpoint": "/auth/login"})
        logger.info("Login success user=%s latency_ms=%.2f", req.username, elapsed)
        return TokenResponse(access_token=token)


@app.post("/auth/logout")
async def logout(authorization: str = Header(default="")):
    with tracer.start_as_current_span("auth.logout") as span:
        try:
            token = authorization.replace("Bearer ", "")
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            active_sessions.add(-1, {"role": payload.get("role", "user")})
            logger.info("Logout user=%s", payload.get("sub"))
            return {"status": "logged_out"}
        except Exception as exc:
            span.record_exception(exc)
            error_counter.add(1, {"reason": "logout_error"})
            raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/auth/validate")
async def validate_token(authorization: str = Header(default="")):
    with tracer.start_as_current_span("auth.validate") as span:
        try:
            token = authorization.replace("Bearer ", "")
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            span.set_attribute("user.name", payload.get("sub", ""))
            return {"valid": True, "user": payload.get("sub"), "role": payload.get("role")}
        except jwt.ExpiredSignatureError:
            error_counter.add(1, {"reason": "token_expired"})
            raise HTTPException(status_code=401, detail="Token expired")
        except Exception as exc:
            span.record_exception(exc)
            error_counter.add(1, {"reason": "token_invalid"})
            raise HTTPException(status_code=401, detail="Invalid token")


@app.post("/auth/order-proxy")
async def proxy_to_order(authorization: str = Header(default="")):
    """Demonstrate cross-service call so the trace propagation is captured."""
    with tracer.start_as_current_span("auth.order_proxy") as span:
        await validate_token(authorization)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ORDER_URL}/orders", headers={"Authorization": authorization}, timeout=5)
        span.set_attribute("upstream.status", resp.status_code)
        return resp.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
