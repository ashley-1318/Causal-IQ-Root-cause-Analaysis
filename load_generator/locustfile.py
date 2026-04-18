"""
CausalIQ Load Generator
Uses gevent-based async requests to simulate realistic traffic
with cascading fault injection via the payment service admin API.
"""
import os
import time
import random
import logging
import threading
import argparse
from datetime import datetime

import requests
from locust import HttpUser, task, between, events
from locust.env import Environment
from locust.runners import LocalRunner
import gevent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load-generator")

AUTH_URL    = os.getenv("AUTH_SERVICE_URL",    "http://auth-service:8000")
ORDER_URL   = os.getenv("ORDER_SERVICE_URL",   "http://order-service:8001")
PAYMENT_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8002")

PRODUCTS = ["laptop", "phone", "tablet", "monitor", "keyboard", "mouse", "headset", "webcam"]

# ── Get auth token ─────────────────────────────────────────────────────────────
def get_token(username="alice", password="pass123") -> str:
    try:
        r = requests.post(f"{AUTH_URL}/auth/login",
                          json={"username": username, "password": password}, timeout=5)
        if r.status_code == 200:
            return r.json().get("access_token", "")
    except Exception as exc:
        logger.warning("Auth failed: %s", exc)
    return ""

# ── Fault injection ────────────────────────────────────────────────────────────
def inject_fault(active: bool, db_latency_ms: int = 500):
    try:
        r = requests.post(f"{PAYMENT_URL}/admin/fault",
                          json={"active": active, "db_latency_ms": db_latency_ms}, timeout=5)
        logger.info("Fault injection %s: %s", "ON" if active else "OFF", r.json())
    except Exception as exc:
        logger.warning("Fault inject failed: %s", exc)


# ── Locust User ────────────────────────────────────────────────────────────────
class CausalIQUser(HttpUser):
    """Simulates realistic user traffic across the microservice stack."""
    host = ORDER_URL
    wait_time = between(0.5, 2.0)

    def on_start(self):
        """Each virtual user gets its own auth token."""
        users = [("alice", "pass123"), ("bob", "pass456"), ("carol", "pass789")]
        u, p = random.choice(users)
        try:
            r = self.client.post(f"{AUTH_URL}/auth/login", json={"username": u, "password": p}, timeout=5)
            if r.status_code == 200:
                self.token = r.json().get("access_token", "")
            else:
                self.token = ""
        except Exception:
            self.token = ""

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @task(5)
    def create_order(self):
        self.client.post(
            f"{ORDER_URL}/orders",
            json={
                "product_id": random.choice(PRODUCTS),
                "quantity":   random.randint(1, 5),
                "amount":     round(random.uniform(9.99, 999.99), 2),
            },
            headers=self._auth_headers(),
            name="POST /orders",
        )

    @task(3)
    def list_orders(self):
        self.client.get(f"{ORDER_URL}/orders", headers=self._auth_headers(), name="GET /orders")

    @task(2)
    def validate_token(self):
        self.client.get(f"{AUTH_URL}/auth/validate", headers=self._auth_headers(), name="GET /auth/validate")

    @task(1)
    def health_check(self):
        for url in [AUTH_URL, ORDER_URL, PAYMENT_URL]:
            self.client.get(f"{url}/health", name="GET /health")


# ── Orchestration ──────────────────────────────────────────────────────────────
def run_scenario(
    users: int = 20,
    duration: int = 120,
    fault_at: int = 30,
    fault_duration: int = 60,
    fault_latency_ms: int = 800,
):
    """
    Full incident simulation scenario:
    1. Ramp up load
    2. Inject DB fault halfway through
    3. Let the AI detect and analyse
    4. Turn off fault and observe recovery
    """
    logger.info("=== CausalIQ Incident Simulation ===")
    logger.info("Users: %d | Duration: %ds | Fault at: %ds | Fault latency: %dms",
                users, duration, fault_at, fault_latency_ms)

    env      = Environment(user_classes=[CausalIQUser], events=events)
    runner   = env.create_local_runner()

    runner.start(users, spawn_rate=5)
    logger.info("Load generator started — %d concurrent users", users)

    # Wait before injecting fault
    time.sleep(fault_at)
    logger.info("Injecting DB fault (latency=%dms)...", fault_latency_ms)
    inject_fault(active=True, db_latency_ms=fault_latency_ms)

    # Keep fault active
    time.sleep(fault_duration)
    logger.info("Removing fault injection — observing recovery...")
    inject_fault(active=False)

    # Wait for remainder
    remaining = duration - fault_at - fault_duration
    if remaining > 0:
        time.sleep(remaining)

    runner.quit()
    logger.info("Load scenario complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CausalIQ Load Generator")
    parser.add_argument("--users",          type=int, default=20)
    parser.add_argument("--duration",       type=int, default=120)
    parser.add_argument("--fault-at",       type=int, default=30,  dest="fault_at")
    parser.add_argument("--fault-duration", type=int, default=60,  dest="fault_duration")
    parser.add_argument("--fault-latency",  type=int, default=800, dest="fault_latency_ms")
    args = parser.parse_args()

    # Wait for services to be ready
    logger.info("Waiting for services to be ready...")
    for _ in range(30):
        try:
            if requests.get(f"{AUTH_URL}/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        logger.error("Services not ready after 150s. Exiting.")
        exit(1)

    logger.info("Services ready. Starting scenario.")
    run_scenario(
        users=args.users,
        duration=args.duration,
        fault_at=args.fault_at,
        fault_duration=args.fault_duration,
        fault_latency_ms=args.fault_latency_ms,
    )
