"""
CausalIQ Phase 5 Validation Harness
────────────────────────────────────
Generates synthetic fault scenarios for ALL 5 fault families,
feeds them through the ensemble detector, and validates:
  1. missed_detection_rate < threshold (accuracy gate)
  2. per-family detection recall
  3. overall F1 score

Usage:
    python phase5_validation.py [--iterations 500] [--max-missed-rate 0.10]
"""
import sys
import os
import json
import random
import argparse
import logging
from datetime import datetime
from typing import Dict, List, Tuple

# Add parent dir so imports work when running standalone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anomaly.detector import (
    EnsembleAnomalyDetector,
    AccuracyGate,
    FAULT_FAMILY_SIGNATURES,
    FEATURE_COLS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("phase5-validation")


# ── Synthetic Fault Generators ────────────────────────────────────────────────
# Each generator returns (feature_dict, is_actually_anomalous, fault_family)

def generate_healthy() -> Tuple[dict, bool, str]:
    """Normal operating conditions."""
    return {
        "service": random.choice(["auth-service", "order-service", "payment-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(35, 10),
        "p99_latency_ms": random.gauss(80, 20),
        "error_rate": max(0, random.gauss(0.01, 0.005)),
        "throughput_rps": random.gauss(50, 10),
    }, False, "healthy"


def generate_db_latency() -> Tuple[dict, bool, str]:
    """DB connection pool exhaustion — high latency, high p99, moderate errors."""
    return {
        "service": random.choice(["payment-service", "order-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(450, 100),
        "p99_latency_ms": random.gauss(900, 150),
        "error_rate": max(0.05, random.gauss(0.15, 0.05)),
        "throughput_rps": random.gauss(15, 5),
    }, True, "db-latency"


def generate_memory_leak() -> Tuple[dict, bool, str]:
    """Gradual degradation: throughput drops, latency rises, errors stay low initially."""
    return {
        "service": random.choice(["auth-service", "order-service", "payment-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(220, 50),
        "p99_latency_ms": random.gauss(400, 80),
        "error_rate": max(0, random.gauss(0.04, 0.02)),
        "throughput_rps": max(0.5, random.gauss(3, 1.5)),
    }, True, "memory-leak"


def generate_cpu_spike() -> Tuple[dict, bool, str]:
    """Sudden latency explosion with very high p99 but initially low errors."""
    return {
        "service": random.choice(["auth-service", "order-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(500, 100),
        "p99_latency_ms": random.gauss(1200, 200),
        "error_rate": max(0, random.gauss(0.03, 0.02)),
        "throughput_rps": random.gauss(25, 8),
    }, True, "cpu-spike"


def generate_network_timeout() -> Tuple[dict, bool, str]:
    """High error rate from upstream failures, moderate latency, low throughput."""
    return {
        "service": random.choice(["order-service", "payment-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(180, 40),
        "p99_latency_ms": random.gauss(350, 70),
        "error_rate": max(0.2, random.gauss(0.35, 0.08)),
        "throughput_rps": max(1, random.gauss(6, 3)),
    }, True, "network-timeout"


def generate_cascading_failure() -> Tuple[dict, bool, str]:
    """Multi-dimensional meltdown: high latency + high errors + high p99."""
    return {
        "service": random.choice(["payment-service", "order-service", "auth-service"]),
        "timestamp": datetime.utcnow().isoformat(),
        "avg_latency_ms": random.gauss(600, 120),
        "p99_latency_ms": random.gauss(1000, 200),
        "error_rate": max(0.15, random.gauss(0.30, 0.08)),
        "throughput_rps": max(1, random.gauss(8, 4)),
    }, True, "cascading-failure"


GENERATORS = {
    "healthy": generate_healthy,
    "db-latency": generate_db_latency,
    "memory-leak": generate_memory_leak,
    "cpu-spike": generate_cpu_spike,
    "network-timeout": generate_network_timeout,
    "cascading-failure": generate_cascading_failure,
}


def run_validation(iterations: int, max_missed_rate: float) -> Dict:
    """
    Run Phase 5 validation harness.
    Returns full report dict with pass/fail status.
    """
    detector = EnsembleAnomalyDetector()
    gate = AccuracyGate(window_size=iterations, max_missed_rate=max_missed_rate)

    # Distribution: ~40% healthy, ~60% faults spread across families
    fault_families = ["db-latency", "memory-leak", "cpu-spike", "network-timeout", "cascading-failure"]
    
    # Phase 1: Warm up the detector with healthy data so it learns baseline
    logger.info("Phase 1: Warming up detector with %d healthy samples...", 50)
    for _ in range(50):
        feature, _, _ = generate_healthy()
        detector.ingest(feature)

    # Phase 2: Run mixed scenario
    logger.info("Phase 2: Running %d mixed fault/healthy iterations...", iterations)
    results_log = []
    
    for i in range(iterations):
        # 40% healthy, 60% faults (12% each family)
        roll = random.random()
        if roll < 0.40:
            feature, is_anomaly, family = generate_healthy()
        else:
            family = random.choice(fault_families)
            feature, is_anomaly, family = GENERATORS[family]()

        result = detector.ingest(feature)
        predicted = result.get("anomaly", False)
        
        gate.record(predicted, is_anomaly, family)
        
        results_log.append({
            "iteration": i,
            "service": feature.get("service"),
            "actual_anomaly": is_anomaly,
            "predicted_anomaly": predicted,
            "fault_family": family,
            "ensemble_votes": result.get("ensemble_votes", 0),
            "score": result.get("score", 0),
            "detected_family": result.get("fault_family", "unknown"),
        })

    # Generate report
    summary = gate.summary()
    
    # Per-family recall calculation
    family_recall = {}
    for fname in fault_families:
        stats = summary["per_family"].get(fname, {"tp": 0, "fn": 0})
        tp = stats.get("tp", 0)
        fn = stats.get("fn", 0)
        total_actual = tp + fn
        recall = tp / total_actual if total_actual > 0 else 0.0
        family_recall[fname] = {
            "recall": round(recall, 4),
            "true_positives": tp,
            "false_negatives": fn,
            "total_actual": total_actual,
        }

    report = {
        "phase": "Phase 5 Validation",
        "timestamp": datetime.utcnow().isoformat(),
        "iterations": iterations,
        "max_missed_rate_threshold": max_missed_rate,
        "overall": {
            "missed_detection_rate": summary["missed_detection_rate"],
            "precision": summary["precision"],
            "recall": summary["recall"],
            "f1_score": summary["f1_score"],
            "gate_passes": summary["gate_passes"],
        },
        "per_family": family_recall,
        "detector_config": {
            "anomaly_score_threshold": float(os.getenv("ANOMALY_SCORE_THRESHOLD", "-0.05")),
            "zscore_threshold": float(os.getenv("ZSCORE_THRESHOLD", "2.5")),
            "ewma_drift_threshold": float(os.getenv("EWMA_DRIFT_THRESHOLD", "1.8")),
            "ensemble_quorum": int(os.getenv("ENSEMBLE_QUORUM", "2")),
            "isolation_forest_contamination": 0.08,
            "isolation_forest_estimators": 300,
        },
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="CausalIQ Phase 5 Validation Harness")
    parser.add_argument("--iterations", type=int, default=500, help="Number of test iterations")
    parser.add_argument("--max-missed-rate", type=float, default=0.10, dest="max_missed_rate",
                       help="Maximum acceptable missed detection rate (0.10 = 10%%)")
    parser.add_argument("--output", type=str, default=None, help="Path to save JSON report")
    args = parser.parse_args()

    logger.info("═" * 60)
    logger.info("  CausalIQ Phase 5 Validation Harness")
    logger.info("  Iterations: %d | Max missed rate: %.1f%%", args.iterations, args.max_missed_rate * 100)
    logger.info("═" * 60)

    report = run_validation(args.iterations, args.max_missed_rate)

    # Pretty print results
    overall = report["overall"]
    gate_status = "✅ PASS" if overall["gate_passes"] else "❌ FAIL"

    logger.info("─" * 60)
    logger.info("  RESULTS: %s", gate_status)
    logger.info("─" * 60)
    logger.info("  Missed Detection Rate:  %.2f%%  (threshold: %.2f%%)",
                overall["missed_detection_rate"] * 100, args.max_missed_rate * 100)
    logger.info("  Precision:              %.2f%%", overall["precision"] * 100)
    logger.info("  Recall:                 %.2f%%", overall["recall"] * 100)
    logger.info("  F1 Score:               %.4f", overall["f1_score"])
    logger.info("")
    logger.info("  Per-Family Recall:")
    for fname, stats in report["per_family"].items():
        recall_pct = stats["recall"] * 100
        status = "✅" if recall_pct >= 80 else "⚠️" if recall_pct >= 60 else "❌"
        logger.info("    %s %-20s  recall=%.1f%%  (TP=%d FN=%d)",
                     status, fname, recall_pct, stats["true_positives"], stats["false_negatives"])
    logger.info("─" * 60)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report saved to %s", args.output)

    # Exit with error code if gate fails
    if not overall["gate_passes"]:
        logger.error("ACCURACY GATE FAILED. Tune thresholds and rerun.")
        sys.exit(1)
    else:
        logger.info("ACCURACY GATE PASSED. Phase 5 validated.")
        sys.exit(0)


if __name__ == "__main__":
    main()
