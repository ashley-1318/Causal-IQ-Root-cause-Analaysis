#!/usr/bin/env python3
"""
CausalIQ Phase 5: Real-World Testing Runner

Runs staged RCA drills, captures MTTR-oriented metrics, records operator feedback,
and evaluates go-live gates.

This script builds on the existing Phase 1 benchmark helpers.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase1_benchmark import (
    DEFAULT_BACKEND,
    parse_iso,
    request_json,
    collect_new_incidents,
    start_load,
    summarize,
    wait_for_backend,
    Scenario,
)


@dataclass(frozen=True)
class DrillScenario:
    name: str
    scenario: Scenario
    manual_mttr_seconds: int
    operator_validation_seconds: int
    ticket_required: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pick_best_incident(incidents):
    if not incidents:
        return None
    return max(incidents, key=lambda item: item.confidence)


def submit_feedback(base_url: str, incident_id: str, is_accurate: bool, expected_root: str, predicted_root: str, note: str) -> dict[str, Any]:
    payload = {
        "is_accurate": is_accurate,
        "actual_root_cause": expected_root if expected_root else predicted_root,
        "operator_feedback": note,
        "verified_by": "phase5-runner",
    }
    return request_json("POST", f"{base_url}/incidents/{incident_id}/feedback", payload=payload, timeout=20)


def run_drill(base_url: str, drill: DrillScenario, grace_seconds: int) -> dict[str, Any]:
    started_at = utc_now()
    trigger_response = start_load(base_url, drill.scenario)

    incidents = collect_new_incidents(
        base_url,
        started_at,
        timeout_seconds=drill.scenario.duration_seconds + grace_seconds,
    )

    best = pick_best_incident(incidents)

    detection_latency = None
    ticket_latency = None
    is_accurate = False
    feedback_result = None

    if incidents:
        detection_latency = round((incidents[0].created_at - started_at).total_seconds(), 2)

    if best and best.ticket_created_at:
        try:
            ticket_latency = round((parse_iso(best.ticket_created_at) - best.created_at).total_seconds(), 2)
        except Exception:
            ticket_latency = None

    if best and drill.scenario.expected_root_cause:
        is_accurate = best.root_cause == drill.scenario.expected_root_cause
        feedback_note = (
            f"Phase5 drill={drill.name}; expected={drill.scenario.expected_root_cause}; "
            f"predicted={best.root_cause}; confidence={best.confidence:.4f}"
        )
        feedback_result = submit_feedback(
            base_url=base_url,
            incident_id=best.incident_id,
            is_accurate=is_accurate,
            expected_root=drill.scenario.expected_root_cause,
            predicted_root=best.root_cause,
            note=feedback_note,
        )

    mttr_with_causaliq = None
    mttr_reduction_ratio = None
    if detection_latency is not None:
        mttr_with_causaliq = detection_latency + (ticket_latency or 0) + drill.operator_validation_seconds
        mttr_reduction_ratio = round(
            1.0 - (mttr_with_causaliq / float(drill.manual_mttr_seconds)),
            4,
        )

    return {
        "drill": drill.name,
        "trigger_response": trigger_response,
        "incident_count": len(incidents),
        "best_incident": {
            "incident_id": best.incident_id,
            "root_cause": best.root_cause,
            "confidence": round(best.confidence, 4),
            "ticket_id": best.ticket_id,
            "ticket_status": best.ticket_status,
            "ticket_source": best.ticket_source,
        } if best else None,
        "expected_root_cause": drill.scenario.expected_root_cause,
        "is_accurate": is_accurate,
        "detection_latency_s": detection_latency,
        "ticket_latency_s": ticket_latency,
        "operator_validation_seconds": drill.operator_validation_seconds,
        "manual_mttr_seconds": drill.manual_mttr_seconds,
        "mttr_with_causaliq_seconds": round(mttr_with_causaliq, 2) if mttr_with_causaliq is not None else None,
        "mttr_reduction_ratio": mttr_reduction_ratio,
        "feedback_result": feedback_result,
        "missed_detection": drill.scenario.inject_fault and len(incidents) == 0,
        "false_positive": (not drill.scenario.inject_fault) and len(incidents) > 0,
        "ticket_required": drill.ticket_required,
        "ticket_created": bool(best and best.ticket_id),
    }


def build_drills(args: argparse.Namespace) -> list[DrillScenario]:
    drills: list[DrillScenario] = [
        DrillScenario(
            name="baseline-sanity",
            scenario=Scenario(
                name="baseline-sanity",
                duration_seconds=args.duration,
                concurrency=args.concurrency,
                inject_fault=False,
                fault_db_latency_ms=50,
                fault_error_rate=0.0,
                fault_family="payment",
                expected_root_cause=None,
            ),
            manual_mttr_seconds=args.manual_mttr_seconds,
            operator_validation_seconds=args.operator_validation_seconds,
            ticket_required=False,
        )
    ]

    for i in range(args.iterations):
        latency = args.fault_latency + (i * args.fault_step_ms)
        drills.append(
            DrillScenario(
                name=f"payment-latency-drill-{i + 1}",
                scenario=Scenario(
                    name=f"payment-latency-drill-{i + 1}",
                    duration_seconds=args.duration,
                    concurrency=args.concurrency,
                    inject_fault=True,
                    fault_db_latency_ms=latency,
                    fault_error_rate=0.25,
                    fault_family="payment",
                    expected_root_cause="payment-service",
                ),
                manual_mttr_seconds=args.manual_mttr_seconds,
                operator_validation_seconds=args.operator_validation_seconds,
                ticket_required=False,
            )
        )

        drills.append(
            DrillScenario(
                name=f"auth-degradation-drill-{i + 1}",
                scenario=Scenario(
                    name=f"auth-degradation-drill-{i + 1}",
                    duration_seconds=args.duration,
                    concurrency=args.concurrency,
                    inject_fault=True,
                    fault_db_latency_ms=latency,
                    fault_error_rate=0.30,
                    fault_family="auth",
                    expected_root_cause="auth-service",
                ),
                manual_mttr_seconds=args.manual_mttr_seconds,
                operator_validation_seconds=args.operator_validation_seconds,
                ticket_required=False,
            )
        )

        drills.append(
            DrillScenario(
                name=f"order-degradation-drill-{i + 1}",
                scenario=Scenario(
                    name=f"order-degradation-drill-{i + 1}",
                    duration_seconds=args.duration,
                    concurrency=args.concurrency,
                    inject_fault=True,
                    fault_db_latency_ms=latency,
                    fault_error_rate=0.30,
                    fault_family="order",
                    expected_root_cause="order-service",
                ),
                manual_mttr_seconds=args.manual_mttr_seconds,
                operator_validation_seconds=args.operator_validation_seconds,
                ticket_required=False,
            )
        )

    return drills


def evaluate_gates(drill_results: list[dict[str, Any]], min_accuracy: float, max_missed_detection: float, min_mttr_reduction: float) -> dict[str, Any]:
    fault_runs = [r for r in drill_results if r["expected_root_cause"]]
    baseline_runs = [r for r in drill_results if not r["expected_root_cause"]]

    accuracy_rate = (
        sum(1 for r in fault_runs if r["is_accurate"]) / len(fault_runs)
        if fault_runs else 0.0
    )
    missed_detection_rate = (
        sum(1 for r in fault_runs if r["missed_detection"]) / len(fault_runs)
        if fault_runs else 0.0
    )
    false_positive_rate = (
        sum(1 for r in baseline_runs if r["false_positive"]) / len(baseline_runs)
        if baseline_runs else 0.0
    )

    mttr_reductions = [r["mttr_reduction_ratio"] for r in fault_runs if r["mttr_reduction_ratio"] is not None]
    avg_mttr_reduction = sum(mttr_reductions) / len(mttr_reductions) if mttr_reductions else 0.0

    gates = {
        "accuracy_gate": accuracy_rate >= min_accuracy,
        "missed_detection_gate": missed_detection_rate <= max_missed_detection,
        "mttr_gate": avg_mttr_reduction >= min_mttr_reduction,
    }

    return {
        "accuracy_rate": round(accuracy_rate, 4),
        "missed_detection_rate": round(missed_detection_rate, 4),
        "false_positive_rate": round(false_positive_rate, 4),
        "average_mttr_reduction_ratio": round(avg_mttr_reduction, 4),
        "gates": gates,
        "go_live_ready": all(gates.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CausalIQ Phase 5 real-world test runner")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="Backend base URL")
    parser.add_argument("--duration", type=int, default=30, help="Duration per drill in seconds")
    parser.add_argument("--concurrency", type=int, default=20, help="Load concurrency")
    parser.add_argument("--iterations", type=int, default=5, help="Number of fault drills")
    parser.add_argument("--fault-latency", type=int, default=700, help="Starting injected DB latency in ms")
    parser.add_argument("--fault-step-ms", type=int, default=50, help="Additional latency per iteration")
    parser.add_argument("--grace-seconds", type=int, default=90, help="Extra collection window")
    parser.add_argument("--operator-validation-seconds", type=int, default=45, help="Operator review time budget")
    parser.add_argument("--manual-mttr-seconds", type=int, default=1800, help="Manual MTTR baseline in seconds")
    parser.add_argument("--min-accuracy", type=float, default=0.85, help="Gate: minimum top-1 accuracy")
    parser.add_argument("--max-missed-detection", type=float, default=0.2, help="Gate: maximum missed detection rate")
    parser.add_argument("--min-mttr-reduction", type=float, default=0.5, help="Gate: minimum average MTTR reduction")
    parser.add_argument("--output", default="", help="Optional output report path")
    parser.add_argument("--quick", action="store_true", help="Quick validation mode (1 iteration, short duration)")
    args = parser.parse_args()

    if args.quick:
        args.duration = 2
        args.concurrency = 2
        args.iterations = 1
        args.grace_seconds = 5
        args.fault_latency = 200
        args.fault_step_ms = 0

    wait_for_backend(args.backend)
    live_accuracy_before = request_json("GET", f"{args.backend}/accuracy-metrics", timeout=20)
    drills = build_drills(args)

    print(f"Running {len(drills)} drills against {args.backend}")

    results: list[dict[str, Any]] = []
    for drill in drills:
        print(f"\n=== Drill: {drill.name} ===")
        result = run_drill(args.backend, drill, args.grace_seconds)
        print(json.dumps(result, indent=2))
        results.append(result)

    # Reuse Phase 1 summary shape where applicable for continuity.
    phase1_like = summarize([
        {
            "inject_fault": bool(r["expected_root_cause"]),
            "accuracy": 1.0 if r["is_accurate"] else 0.0,
            "false_positive": r["false_positive"],
            "missed_detection": r["missed_detection"],
            "first_incident_latency_s": r["detection_latency_s"],
            "ticket_latency_s": r["ticket_latency_s"],
            "mean_confidence": (r["best_incident"] or {}).get("confidence", 0.0),
        }
        for r in results
    ])

    gate_summary = evaluate_gates(
        drill_results=results,
        min_accuracy=args.min_accuracy,
        max_missed_detection=args.max_missed_detection,
        min_mttr_reduction=args.min_mttr_reduction,
    )

    live_accuracy = request_json("GET", f"{args.backend}/accuracy-metrics", timeout=20)
    resilience = request_json("GET", f"{args.backend}/resilience", timeout=20)

    before_total = int((live_accuracy_before.get("overall") or {}).get("total", 0))
    after_total = int((live_accuracy.get("overall") or {}).get("total", 0))
    feedback_samples_added = max(0, after_total - before_total)

    report = {
        "generated_at": utc_now().isoformat(),
        "backend": args.backend,
        "phase1_like_summary": phase1_like,
        "phase5_gate_summary": gate_summary,
        "feedback_samples_added": feedback_samples_added,
        "live_accuracy_metrics_before": live_accuracy_before,
        "live_accuracy_metrics": live_accuracy,
        "resilience_snapshot": resilience,
        "drills": results,
    }

    print("\n=== Phase 5 Gate Summary ===")
    print(json.dumps(gate_summary, indent=2))

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written to {output_path}")

    return 0 if gate_summary["go_live_ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
