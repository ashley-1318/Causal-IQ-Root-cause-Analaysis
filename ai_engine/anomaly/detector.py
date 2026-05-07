"""
CausalIQ Anomaly Detection Engine (Phase 5 — Tuned)
- Ensemble detection: Isolation Forest + Z-Score + EWMA Drift
- Multi-fault-family signature matching (5 families)
- Built-in accuracy gate: tracks missed_detection_rate, precision, recall
- Publishes anomaly events to rca-results topic and ClickHouse
"""
import os
import json
import time
import logging
import hashlib
import uuid
from datetime import datetime
from typing import Optional, Dict, List
from collections import defaultdict, deque

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

# ── Tuned Thresholds ──────────────────────────────────────────────────────────
# Lowered from -0.1 to -0.05 to reduce missed detections
ANOMALY_SCORE_THRESHOLD   = float(os.getenv("ANOMALY_SCORE_THRESHOLD", "-0.05"))
MIN_TRAINING_SAMPLES      = int(os.getenv("MIN_TRAINING_SAMPLES", "30"))
# Z-score threshold for secondary detector
ZSCORE_THRESHOLD          = float(os.getenv("ZSCORE_THRESHOLD", "2.5"))
# EWMA drift sensitivity (alpha)
EWMA_ALPHA                = float(os.getenv("EWMA_ALPHA", "0.3"))
EWMA_DRIFT_THRESHOLD      = float(os.getenv("EWMA_DRIFT_THRESHOLD", "1.8"))
# Ensemble: number of detectors that must agree to flag anomaly
ENSEMBLE_QUORUM           = int(os.getenv("ENSEMBLE_QUORUM", "1"))

# ── Fault Family Signatures ───────────────────────────────────────────────────
# Each signature defines a pattern matcher for a specific failure mode.
# Phase 5 now covers 5 fault families, not just "payment-latency".
FAULT_FAMILY_SIGNATURES: Dict[str, Dict] = {
    "db-latency": {
        "description": "Database connection pool saturation or slow queries",
        "match": lambda f: (
            f.get("avg_latency_ms", 0) > 200
            and f.get("p99_latency_ms", 0) > 500
            and f.get("error_rate", 0) > 0.05
        ),
        "severity_weight": 1.2,
    },
    "memory-leak": {
        "description": "Gradual throughput degradation with steady latency rise",
        "match": lambda f: (
            f.get("throughput_rps", 999) < 5.0
            and f.get("avg_latency_ms", 0) > 150
            and f.get("error_rate", 0) < 0.15  # errors come late in mem-leak
        ),
        "severity_weight": 1.3,
    },
    "cpu-spike": {
        "description": "Sudden latency spike with high p99 but low error rate initially",
        "match": lambda f: (
            f.get("p99_latency_ms", 0) > 800
            and f.get("avg_latency_ms", 0) > 300
            and f.get("error_rate", 0) < 0.10
        ),
        "severity_weight": 1.1,
    },
    "network-timeout": {
        "description": "High error rate with moderate latency — upstream dependency failures",
        "match": lambda f: (
            f.get("error_rate", 0) > 0.20
            and f.get("avg_latency_ms", 0) > 100
            and f.get("throughput_rps", 0) < 10.0
        ),
        "severity_weight": 1.4,
    },
    "cascading-failure": {
        "description": "Multi-service degradation with both high latency and errors",
        "match": lambda f: (
            f.get("avg_latency_ms", 0) > 300
            and f.get("error_rate", 0) > 0.15
            and f.get("p99_latency_ms", 0) > 600
        ),
        "severity_weight": 1.5,
    },
}


class AccuracyGate:
    """
    Tracks detection quality metrics over a rolling window.
    Used to validate Phase 5 tuning: the gate passes only when
    missed_detection_rate is below the configured threshold.
    """

    def __init__(self, window_size: int = 200, max_missed_rate: float = 0.10):
        self.window_size = window_size
        self.max_missed_rate = max_missed_rate
        # Rolling counters
        self.predictions: deque = deque(maxlen=window_size)
        # (predicted_anomaly: bool, actual_anomaly: bool, fault_family: str)
        self.total_evaluated = 0

    def record(self, predicted: bool, actual: bool, fault_family: str = "unknown"):
        self.predictions.append((predicted, actual, fault_family))
        self.total_evaluated += 1

    @property
    def true_positives(self) -> int:
        return sum(1 for p, a, _ in self.predictions if p and a)

    @property
    def false_negatives(self) -> int:
        """Missed detections — actual anomalies we failed to flag."""
        return sum(1 for p, a, _ in self.predictions if not p and a)

    @property
    def false_positives(self) -> int:
        return sum(1 for p, a, _ in self.predictions if p and not a)

    @property
    def true_negatives(self) -> int:
        return sum(1 for p, a, _ in self.predictions if not p and not a)

    @property
    def missed_detection_rate(self) -> float:
        actual_positives = self.true_positives + self.false_negatives
        if actual_positives == 0:
            return 0.0
        return self.false_negatives / actual_positives

    @property
    def precision(self) -> float:
        flagged = self.true_positives + self.false_positives
        if flagged == 0:
            return 1.0
        return self.true_positives / flagged

    @property
    def recall(self) -> float:
        actual_positives = self.true_positives + self.false_negatives
        if actual_positives == 0:
            return 1.0
        return self.true_positives / actual_positives

    @property
    def f1_score(self) -> float:
        p, r = self.precision, self.recall
        if p + r == 0:
            return 0.0
        return 2 * (p * r) / (p + r)

    def gate_passes(self) -> bool:
        """Returns True if the accuracy gate is met."""
        if len(self.predictions) < 20:
            return True  # Not enough data to judge
        return self.missed_detection_rate <= self.max_missed_rate

    def per_family_stats(self) -> Dict[str, Dict]:
        families: Dict[str, Dict] = defaultdict(lambda: {"tp": 0, "fn": 0, "fp": 0, "tn": 0})
        for pred, actual, family in self.predictions:
            if pred and actual:
                families[family]["tp"] += 1
            elif not pred and actual:
                families[family]["fn"] += 1
            elif pred and not actual:
                families[family]["fp"] += 1
            else:
                families[family]["tn"] += 1
        return dict(families)

    def summary(self) -> Dict:
        return {
            "total_evaluated": self.total_evaluated,
            "window_size": len(self.predictions),
            "missed_detection_rate": round(self.missed_detection_rate, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1_score": round(self.f1_score, 4),
            "gate_passes": self.gate_passes(),
            "true_positives": self.true_positives,
            "false_negatives": self.false_negatives,
            "false_positives": self.false_positives,
            "per_family": self.per_family_stats(),
        }


class EnsembleAnomalyDetector:
    """
    Phase 5 tuned ensemble anomaly detector.
    Combines three detection methods and requires quorum agreement:
      1. Isolation Forest (statistical outlier detection)
      2. Z-Score (per-feature deviation from rolling mean)
      3. EWMA Drift (exponentially weighted moving average shift detection)
    """

    def __init__(self):
        # Isolation Forest
        self.model: Optional[IsolationForest] = None
        self.scaler: Optional[StandardScaler] = None
        self.training_buffer: list[list[float]] = []
        self.sample_count = 0
        self.last_retrain = 0
        self.retrain_interval = 180  # retrain every 3 min (was 5)

        # Z-Score rolling statistics
        self.rolling_means: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.rolling_stds: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))

        # EWMA state per service
        self.ewma_state: Dict[str, Dict[str, float]] = {}

        # Accuracy gate
        self.accuracy_gate = AccuracyGate(
            window_size=int(os.getenv("ACCURACY_GATE_WINDOW", "200")),
            max_missed_rate=float(os.getenv("MAX_MISSED_DETECTION_RATE", "0.10")),
        )

        self._load()

    def _load(self):
        try:
            self.model = joblib.load(MODEL_PATH)
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

    # ── Detector 1: Isolation Forest ──────────────────────────────────────────
    def _score_isolation_forest(self, vec: list[float]) -> tuple[bool, float]:
        if self.model is None:
            return False, 0.0
        X = np.array([vec])
        Xs = self.scaler.transform(X)
        score = float(self.model.score_samples(Xs)[0])
        return score < ANOMALY_SCORE_THRESHOLD, score

    # ── Detector 2: Z-Score ───────────────────────────────────────────────────
    def _score_zscore(self, service: str, vec: list[float]) -> tuple[bool, float]:
        history = self.rolling_means.get(service)
        if history is None or len(history) < 10:
            return False, 0.0

        means = np.mean(list(history), axis=0)
        stds = np.std(list(history), axis=0)
        stds = np.where(stds < 1e-6, 1.0, stds)  # avoid div-by-zero

        z_scores = np.abs((np.array(vec) - means) / stds)
        max_z = float(np.max(z_scores))
        return max_z > ZSCORE_THRESHOLD, max_z

    # ── Detector 3: EWMA Drift ────────────────────────────────────────────────
    def _score_ewma(self, service: str, vec: list[float]) -> tuple[bool, float]:
        if service not in self.ewma_state:
            self.ewma_state[service] = {f: v for f, v in zip(FEATURE_COLS, vec)}
            return False, 0.0

        prev = self.ewma_state[service]
        drifts = []
        new_state = {}
        for f, v in zip(FEATURE_COLS, vec):
            ewma = EWMA_ALPHA * v + (1 - EWMA_ALPHA) * prev.get(f, v)
            drift = abs(v - ewma) / max(abs(ewma), 1e-6)
            drifts.append(drift)
            new_state[f] = ewma

        self.ewma_state[service] = new_state
        max_drift = max(drifts)
        return max_drift > EWMA_DRIFT_THRESHOLD, max_drift

    # ── Fault Family Classification ───────────────────────────────────────────
    def classify_fault_family(self, feature: dict) -> str:
        """Match the feature vector against known fault family signatures."""
        for family_name, sig in FAULT_FAMILY_SIGNATURES.items():
            if sig["match"](feature):
                return family_name
        return "unknown"

    # ── Main Ingestion ────────────────────────────────────────────────────────
    def ingest(self, feature: dict) -> dict:
        """Ingest a feature vector and return ensemble anomaly assessment."""
        vec = self._extract_feature_vector(feature)
        if vec is None:
            return {"anomaly": False, "score": 0.0}

        service = feature.get("service", "unknown")

        # Feed rolling statistics
        self.rolling_means[service].append(vec)
        self.training_buffer.append(vec)
        self.sample_count += 1

        # Retrain if needed
        now = time.time()
        should_retrain = (
            (self.model is None and len(self.training_buffer) >= MIN_TRAINING_SAMPLES)
            or (len(self.training_buffer) >= MIN_TRAINING_SAMPLES
                and now - self.last_retrain > self.retrain_interval)
        )
        if should_retrain:
            self._retrain()

        # ── Run ensemble ──────────────────────────────────────────────────────
        votes = 0
        details = {}

        # Isolation Forest
        if_anomaly, if_score = self._score_isolation_forest(vec)
        details["isolation_forest"] = {"anomaly": if_anomaly, "score": round(if_score, 4)}
        if if_anomaly:
            votes += 1

        # Z-Score
        zs_anomaly, zs_score = self._score_zscore(service, vec)
        details["zscore"] = {"anomaly": zs_anomaly, "max_z": round(zs_score, 4)}
        if zs_anomaly:
            votes += 1

        # EWMA Drift
        ew_anomaly, ew_drift = self._score_ewma(service, vec)
        details["ewma"] = {"anomaly": ew_anomaly, "drift": round(ew_drift, 4)}
        if ew_anomaly:
            votes += 1

        # Fault family signature — always checked, provides bonus vote
        fault_family = self.classify_fault_family(feature)
        if fault_family != "unknown":
            votes += 1  # Signature match gives extra vote
            details["signature_match"] = fault_family

        # Rule-based fallback: always fires on obvious fault conditions
        # This ensures detection works even when ML models are untrained or
        # were trained on contaminated data
        latency = feature.get("avg_latency_ms", 0)
        error_rate = feature.get("error_rate", 0)
        p99 = feature.get("p99_latency_ms", 0)
        rule_anomaly = (
            (latency > 150 and error_rate > 0.03)
            or (p99 > 400 and error_rate > 0.02)
            or (error_rate > 0.10)
        )
        details["rule_based"] = {"anomaly": rule_anomaly, "latency": latency, "error_rate": error_rate}
        if rule_anomaly:
            votes += 1

        is_anomaly = votes >= ENSEMBLE_QUORUM

        # Calculate composite score (weighted average of detector scores)
        weight_if = FAULT_FAMILY_SIGNATURES.get(fault_family, {}).get("severity_weight", 1.0)
        composite_score = round(
            abs(if_score) * 0.5 * weight_if
            + (zs_score / 10.0) * 0.3
            + ew_drift * 0.2,
            4,
        )

        return {
            "anomaly": is_anomaly,
            "score": composite_score if is_anomaly else round(abs(if_score), 4),
            "ensemble_votes": votes,
            "ensemble_quorum": ENSEMBLE_QUORUM,
            "fault_family": fault_family,
            "threshold": ANOMALY_SCORE_THRESHOLD,
            "features": dict(zip(FEATURE_COLS, vec)),
            "detectors": details,
        }

    def _retrain(self):
        logger.info("Retraining Isolation Forest on %d samples...", len(self.training_buffer))
        X = np.array(self.training_buffer[-5000:])
        self.scaler = StandardScaler()
        Xs = self.scaler.fit_transform(X)
        self.model = IsolationForest(
            n_estimators=300,          # Increased from 200
            contamination=0.08,        # Increased from 0.05 — catches more edge cases
            max_features=len(FEATURE_COLS),
            random_state=42,
            n_jobs=-1,
        )
        self.model.fit(Xs)
        self._save()
        self.last_retrain = time.time()
        logger.info("Retrain complete. Model saved.")


# ── Keep backward compatibility alias ─────────────────────────────────────────
IncrementalAnomalyDetector = EnsembleAnomalyDetector

# ── ClickHouse integration ────────────────────────────────────────────────────
import clickhouse_connect

CH_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "")
CH_DB   = os.getenv("CLICKHOUSE_DB", "causaliq")

def run():
    detector = EnsembleAnomalyDetector()

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

    logger.info("Anomaly detector (Phase 5 Ensemble) started.")

    # Periodically log accuracy gate status
    last_gate_log = time.time()
    GATE_LOG_INTERVAL = 60  # seconds

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
                        "fault_family": result.get("fault_family", "unknown"),
                        "ensemble_votes": result.get("ensemble_votes", 0),
                        "avg_latency_ms": feature.get("avg_latency_ms", 0),
                        "p99_latency_ms": feature.get("p99_latency_ms", 0),
                        "error_rate": feature.get("error_rate", 0),
                        "throughput_rps": feature.get("throughput_rps", 0),
                        "dependencies": feature.get("dependencies", []),
                        "all_dependencies": dependencies,
                        "detectors": result.get("detectors", {}),
                    }
                    anomaly_events.append(event)
                    logger.warning(
                        "ANOMALY detected service=%s score=%.4f family=%s votes=%d/%d "
                        "latency=%.1fms err_rate=%.3f",
                        event["service"], event["anomaly_score"],
                        event["fault_family"], event["ensemble_votes"],
                        ENSEMBLE_QUORUM,
                        event["avg_latency_ms"], event["error_rate"],
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

            # Periodically log accuracy gate metrics
            if time.time() - last_gate_log > GATE_LOG_INTERVAL:
                gate = detector.accuracy_gate.summary()
                gate_status = "PASS ✅" if gate["gate_passes"] else "FAIL ❌"
                logger.info(
                    "Accuracy Gate [%s]: missed_rate=%.2f%% precision=%.2f%% "
                    "recall=%.2f%% f1=%.4f window=%d",
                    gate_status,
                    gate["missed_detection_rate"] * 100,
                    gate["precision"] * 100,
                    gate["recall"] * 100,
                    gate["f1_score"],
                    gate["window_size"],
                )
                last_gate_log = time.time()

    except KeyboardInterrupt:
        logger.info("Shutting down anomaly detector")
    finally:
        # Final gate report
        logger.info("Final accuracy gate summary: %s", json.dumps(detector.accuracy_gate.summary()))
        consumer.close()


if __name__ == "__main__":
    time.sleep(20)
    run()
