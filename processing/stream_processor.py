"""
CausalIQ Stream Processing Engine
- Consumes from otel-logs, otel-metrics, otel-traces
- Performs sliding-window correlation
- Extracts features for anomaly detection
- Publishes enriched events to the AI engine
"""
import os
import json
import time
import asyncio
import logging
from datetime import datetime
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from typing import Optional

from confluent_kafka import Consumer, Producer, KafkaError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("stream-processor")

KAFKA_BOOTSTRAP    = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
WINDOW_SECONDS     = int(os.getenv("WINDOW_SECONDS", "30"))
ANOMALY_TOPIC      = "anomalies"

# ── Data Structures ────────────────────────────────────────────────────────────
@dataclass
class TelemetryWindow:
    service: str
    latencies: deque  = field(default_factory=lambda: deque(maxlen=1000))
    error_count: int  = 0
    request_count: int = 0
    timestamps: deque = field(default_factory=lambda: deque(maxlen=1000))

    def error_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.error_count / self.request_count

    def avg_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)

    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        idx = int(len(s) * 0.99)
        return s[min(idx, len(s) - 1)]

    def throughput_rps(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        span = (self.timestamps[-1] - self.timestamps[0])
        if span <= 0:
            return 0.0
        return len(self.timestamps) / span


class SlidingWindowCorrelator:
    """Maintains per-service time windows and correlates telemetry signals."""

    def __init__(self, window_seconds: int = 30):
        self.window = window_seconds
        self.windows: dict[str, TelemetryWindow] = defaultdict(
            lambda: TelemetryWindow(service="unknown")
        )
        # Trace correlation: trace_id → list of spans
        self.traces: dict[str, list] = defaultdict(list)
        # Dependency edges discovered from traces
        self.dependencies: dict[str, set] = defaultdict(set)

    def ingest_metric(self, payload: dict):
        """Parse OTLP-JSON metric and update window."""
        try:
            for rm in payload.get("resourceMetrics", []):
                svc = self._extract_service(rm.get("resource", {}))
                win = self.windows[svc]
                win.service = svc
                for sm in rm.get("scopeMetrics", []):
                    for metric in sm.get("metrics", []):
                        name = metric.get("name", "")
                        if "duration" in name or "latency" in name:
                            for dp in metric.get("histogram", {}).get("dataPoints", []):
                                count = int(dp.get("count", 0))
                                s = float(dp.get("sum", 0))
                                if count > 0:
                                    win.latencies.append(s / count)
                                    win.request_count += count
                                    win.timestamps.append(time.time())
                        elif "error" in name:
                            for dp in metric.get("sum", {}).get("dataPoints", []):
                                win.error_count += int(dp.get("asInt", 0))
        except Exception as exc:
            logger.warning("Metric parse error: %s", exc)

    def ingest_trace(self, payload: dict):
        """Parse OTLP-JSON trace and build dependency graph."""
        try:
            for rt in payload.get("resourceSpans", []):
                svc = self._extract_service(rt.get("resource", {}))
                for ss in rt.get("scopeSpans", []):
                    for span in ss.get("spans", []):
                        tid = span.get("traceId", "")
                        pid = span.get("parentSpanId", "")
                        self.traces[tid].append({"service": svc, "span": span})

                        # Infer dependency: if span has parent from different service
                        if pid:
                            for other_tid, spans in self.traces.items():
                                for s in spans:
                                    if s["span"].get("spanId") == pid and s["service"] != svc:
                                        self.dependencies[s["service"]].add(svc)
        except Exception as exc:
            logger.warning("Trace parse error: %s", exc)

    def ingest_log(self, payload: dict):
        """Parse OTLP-JSON log and update error counts."""
        try:
            for rl in payload.get("resourceLogs", []):
                svc = self._extract_service(rl.get("resource", {}))
                win = self.windows[svc]
                for sl in rl.get("scopeLogs", []):
                    for record in sl.get("logRecords", []):
                        severity = int(record.get("severityNumber", 0))
                        if severity >= 17:  # ERROR or above
                            win.error_count += 1
        except Exception as exc:
            logger.warning("Log parse error: %s", exc)

    def get_features(self) -> list[dict]:
        """Extract feature vector for each service window."""
        features = []
        now = time.time()
        for svc, win in self.windows.items():
            features.append({
                "service": svc,
                "timestamp": datetime.utcnow().isoformat(),
                "avg_latency_ms": round(win.avg_latency(), 2),
                "p99_latency_ms": round(win.p99_latency(), 2),
                "error_rate": round(win.error_rate(), 4),
                "request_count": win.request_count,
                "throughput_rps": round(win.throughput_rps(), 2),
                "dependencies": list(self.dependencies.get(svc, [])),
            })
        return features

    @staticmethod
    def _extract_service(resource: dict) -> str:
        for attr in resource.get("attributes", []):
            if attr.get("key") == "service.name":
                return attr.get("value", {}).get("stringValue", "unknown")
        return "unknown"


# ── Kafka Consumer/Producer Setup ─────────────────────────────────────────────
def make_consumer(group_id: str, topics: list[str]) -> Consumer:
    c = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": group_id,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        "max.poll.interval.ms": 300000,
    })
    c.subscribe(topics)
    return c


def make_producer() -> Producer:
    return Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "acks": 1,
        "linger.ms": 100,
        "compression.type": "snappy",
    })


# ── Main Processing Loop ────────────────────────────────────────────────────────
def run():
    correlator = SlidingWindowCorrelator(window_seconds=WINDOW_SECONDS)
    consumer   = make_consumer("stream-processor", ["otel-metrics", "otel-traces", "otel-logs"])
    producer   = make_producer()

    logger.info("Stream processor started. Listening on topics...")
    last_publish = time.time()
    PUBLISH_INTERVAL = 5  # seconds

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                pass
            elif msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error("Kafka error: %s", msg.error())
            else:
                topic = msg.topic()
                try:
                    payload = json.loads(msg.value().decode("utf-8"))
                    if topic == "otel-metrics":
                        correlator.ingest_metric(payload)
                    elif topic == "otel-traces":
                        correlator.ingest_trace(payload)
                    elif topic == "otel-logs":
                        correlator.ingest_log(payload)
                except json.JSONDecodeError:
                    pass  # Skip malformed messages

            # Periodically emit feature vectors to anomaly detector
            if time.time() - last_publish > PUBLISH_INTERVAL:
                features = correlator.get_features()
                if features:
                    payload = json.dumps({
                        "window_seconds": WINDOW_SECONDS,
                        "features": features,
                        "dependencies": {k: list(v) for k, v in correlator.dependencies.items()},
                        "ts": datetime.utcnow().isoformat(),
                    }).encode()
                    producer.produce(ANOMALY_TOPIC, value=payload)
                    producer.flush()
                    logger.info("Published %d feature vectors", len(features))
                last_publish = time.time()

    except KeyboardInterrupt:
        logger.info("Shutting down stream processor")
    finally:
        consumer.close()

if __name__ == "__main__":
    time.sleep(15)  # Wait for Kafka to be ready
    run()
