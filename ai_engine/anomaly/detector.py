"""
CausalIQ Anomaly Detection Engine
- Uses Isolation Forest (scikit-learn) for real-time anomaly scoring
- Trains incrementally on streaming feature vectors
- Publishes anomaly events to rca-results topic and ClickHouse
"""
import os
import json
import time
import logging
import hashlib
from datetime import datetime
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import joblib
from confluent_kafka import Consumer, Producer, KafkaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("anomaly-detector")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
MODEL_PATH      = "/tmp/isolation_forest.pkl"
SCALER_PATH     = "/tmp/scaler.pkl"

# Feature columns in fixed order
FEATURE_COLS = ["avg_latency_ms", "p99_latency_ms", "error_rate", "throughput_rps"]

# Thresholds
ANOMALY_SCORE_THRESHOLD = float(os.getenv("ANOMALY_SCORE_THRESHOLD", "-0.1"))
MIN_TRAINING_SAMPLES    = int(os.getenv("MIN_TRAINING_SAMPLES", "50"))


class IncrementalAnomalyDetector:
    """
    Online anomaly detector using Isolation Forest.
    Retrained periodically on incoming streaming data.
    """
    def __init__(self):
        self.model: Optional[IsolationForest] = None
        self.scaler: Optional[StandardScaler] = None
        self.training_buffer: list[list[float]] = []
        self.sample_count = 0
        self.last_retrain = 0
        self.retrain_interval = 300  # seconds

        # Try to load existing model
        self._load()

    def _load(self):
        try:
            self.model  = joblib.load(MODEL_PATH)
            self.scaler = joblib.load(SCALER_PATH)
            logger.info("Loaded existing model from disk")
        except FileNotFoundError:
            logger.info("No pre-trained model found. Will train on first batch.")

    def _save(self):
        joblib.dump(self.model, MODEL_PATH)
        joblib.dump(self.scaler, SCALER_PATH)

    def _extract_feature_vector(self, feature: dict) -> Optional[list[float]]:
        try:
            return [float(feature.get(c, 0.0)) for c in FEATURE_COLS]
        except (TypeError, ValueError):
            return None

    def ingest(self, feature: dict) -> dict:
        """Ingest a feature vector and return anomaly assessment."""
        vec = self._extract_feature_vector(feature)
        if vec is None:
            return {"anomaly": False, "score": 0.0}

        self.training_buffer.append(vec)
        self.sample_count += 1

        # Retrain if enough data accumulated
        now = time.time()
        should_retrain = (
            (self.model is None and len(self.training_buffer) >= MIN_TRAINING_SAMPLES)
            or (len(self.training_buffer) >= MIN_TRAINING_SAMPLES and now - self.last_retrain > self.retrain_interval)
        )

        if should_retrain:
            self._retrain()

        # Score
        if self.model is None:
            return {"anomaly": False, "score": 0.0, "reason": "model_not_ready"}

        X = np.array([vec])
        Xs = self.scaler.transform(X)
        score = float(self.model.score_samples(Xs)[0])
        is_anomaly = score < ANOMALY_SCORE_THRESHOLD

        return {
            "anomaly": is_anomaly,
            "score": round(score, 4),
            "threshold": ANOMALY_SCORE_THRESHOLD,
            "features": dict(zip(FEATURE_COLS, vec)),
        }

    def _retrain(self):
        logger.info("Retraining Isolation Forest on %d samples...", len(self.training_buffer))
        X = np.array(self.training_buffer[-5000:])  # cap at 5k
        self.scaler = StandardScaler()
        Xs = self.scaler.fit_transform(X)
        self.model = IsolationForest(
            n_estimators=200,
            contamination=0.05,
            max_features=len(FEATURE_COLS),
            random_state=42,
            n_jobs=-1,
        )
        self.model.fit(Xs)
        self._save()
        self.last_retrain = time.time()
        logger.info("Retrain complete. Model saved.")


import clickhouse_connect

CH_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "")
CH_DB   = os.getenv("CLICKHOUSE_DB", "causaliq")

def run():
    detector = IncrementalAnomalyDetector()
    
    # Initialize ClickHouse Client
    try:
        ch_client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT, 
                                               username=CH_USER, password=CH_PASS)
        logger.info("Connected to ClickHouse")
    except Exception as e:
        logger.error("Failed to connect to ClickHouse: %s", e)
        ch_client = None

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "anomaly-detector",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })
    consumer.subscribe(["anomalies"])

    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "acks": 1})

    logger.info("Anomaly detector started.")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error("Kafka error: %s", msg.error())
                continue

            try:
                payload = json.loads(msg.value().decode("utf-8"))
            except json.JSONDecodeError:
                continue

            features = payload.get("features", [])
            dependencies = payload.get("dependencies", {})
            
            # --- Store metrics for Dashboard ---
            if ch_client and features:
                metric_rows = []
                for f in features:
                    metric_rows.append([
                        f.get("service", "unknown"),
                        float(f.get("avg_latency_ms", 0)),
                        float(f.get("p99_latency_ms", 0)),
                        float(f.get("error_rate", 0)),
                        float(f.get("throughput_rps", 0)),
                    ])
                try:
                    ch_client.insert(f"{CH_DB}.service_metrics", metric_rows,
                                   column_names=["service", "avg_latency_ms", "p99_latency_ms", 
                                               "error_rate", "throughput_rps"])
                except Exception as e:
                    logger.error("Failed to insert metrics to ClickHouse: %s", e)

            anomaly_events = []
            for feature in features:
                result = detector.ingest(feature)
                if result.get("anomaly"):
                    event = {
                        "service": feature.get("service", "unknown"),
                        "timestamp": feature.get("timestamp", datetime.utcnow().isoformat()),
                        "anomaly_score": result["score"],
                        "avg_latency_ms": feature.get("avg_latency_ms", 0),
                        "p99_latency_ms": feature.get("p99_latency_ms", 0),
                        "error_rate": feature.get("error_rate", 0),
                        "throughput_rps": feature.get("throughput_rps", 0),
                        "dependencies": feature.get("dependencies", []),
                        "all_dependencies": dependencies,
                    }
                    anomaly_events.append(event)
                    logger.warning(
                        "ANOMALY detected service=%s score=%.4f latency=%.1fms err_rate=%.3f",
                        event["service"], event["anomaly_score"],
                        event["avg_latency_ms"], event["error_rate"]
                    )

            if anomaly_events:
                rca_payload = json.dumps({
                    "anomalies": anomaly_events,
                    "dependencies": dependencies,
                    "window_ts": payload.get("ts"),
                    "detected_at": datetime.utcnow().isoformat(),
                }).encode()
                producer.produce("rca-results", value=rca_payload)
                producer.flush()

    except KeyboardInterrupt:
        logger.info("Shutting down anomaly detector")
    finally:
        consumer.close()


if __name__ == "__main__":
    time.sleep(20)
    run()
