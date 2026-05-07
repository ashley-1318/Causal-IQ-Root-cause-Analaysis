#!/usr/bin/env python3
"""
CausalIQ Phase 1 Benchmark

Runs a small set of controlled scenarios against the existing backend
trigger-load endpoint, then scores the resulting incidents for:

- root cause accuracy
- incident detection latency
- ticket creation latency
- false positives during healthy baselines

The script is intentionally host-side and uses only the standard library so it
can run in the same environment as Docker Desktop / docker compose.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_BACKEND = "http://localhost:9001"
DEFAULT_EXPECTED_ROOT_CAUSE = "payment-service"


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    duration_seconds: int
    concurrency: int
    inject_fault: bool
    fault_db_latency_ms: int
    expected_root_cause: Optional[str] = None
    fault_error_rate: float = 0.20
    fault_family: str = "payment"


@dataclasses.dataclass
class IncidentSample:
    incident_id: str
    root_cause: str
    confidence: float
    created_at: datetime
    ticket_id: str = ""
    ticket_status: str = ""
    ticket_source: str = ""
    ticket_created_at: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def request_json(method: str, url: str, payload: Optional[dict[str, Any]] = None, timeout: int = 15) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def wait_for_backend(base_url: str, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            data = request_json("GET", f"{base_url}/health", timeout=5)
            if data.get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"Backend at {base_url} did not become ready within {timeout_seconds}s")


def fetch_incidents(base_url: str, limit: int = 50) -> list[dict[str, Any]]:
    data = request_json("GET", f"{base_url}/incidents?limit={limit}", timeout=20)
    if not isinstance(data, list):
        return []
    return data


def start_load(base_url: str, scenario: Scenario) -> dict[str, Any]:
    payload = {
        "duration_seconds": scenario.duration_seconds,
        "concurrency": scenario.concurrency,
        "inject_fault": scenario.inject_fault,
        "fault_db_latency_ms": scenario.fault_db_latency_ms,
        "fault_error_rate": scenario.fault_error_rate,
        "fault_family": scenario.fault_family,
    }
    return request_json("POST", f"{base_url}/trigger-load", payload=payload, timeout=20)


def collect_new_incidents(base_url: str, started_at: datetime, poll_seconds: int = 5, timeout_seconds: int = 180) -> list[IncidentSample]:
    deadline = time.time() + timeout_seconds
    seen: dict[str, IncidentSample] = {}

    while time.time() < deadline:
        try:
            incidents = fetch_incidents(base_url, limit=100)
            for raw in incidents:
                created_at_raw = raw.get("created_at")
                incident_id = str(raw.get("incident_id", ""))
                if not incident_id or not created_at_raw:
                    continue

                created_at = parse_iso(created_at_raw)
                if created_at < started_at:
                    continue

                seen[incident_id] = IncidentSample(
                    incident_id=incident_id,
                    root_cause=str(raw.get("root_cause", "")),
                    confidence=float(raw.get("confidence") or 0.0),
                    created_at=created_at,
                    ticket_id=str(raw.get("ticket_id", "") or ""),
                    ticket_status=str(raw.get("ticket_status", "") or ""),
                    ticket_source=str(raw.get("ticket_source", "") or ""),
                    ticket_created_at=str(raw.get("ticket_created_at", "") or ""),
                )
        except Exception:
            pass

        if seen:
            # Keep polling a little longer so we can capture late ticket syncs.
            time.sleep(poll_seconds)
            break

        time.sleep(poll_seconds)

    while time.time() < deadline:
        try:
            incidents = fetch_incidents(base_url, limit=100)
            for raw in incidents:
                created_at_raw = raw.get("created_at")
                incident_id = str(raw.get("incident_id", ""))
                if not incident_id or not created_at_raw:
                    continue
                created_at = parse_iso(created_at_raw)
                if created_at < started_at:
                    continue
                seen[incident_id] = IncidentSample(
                    incident_id=incident_id,
                    root_cause=str(raw.get("root_cause", "")),
                    confidence=float(raw.get("confidence") or 0.0),
                    created_at=created_at,
                    ticket_id=str(raw.get("ticket_id", "") or ""),
                    ticket_status=str(raw.get("ticket_status", "") or ""),
                    ticket_source=str(raw.get("ticket_source", "") or ""),
                    ticket_created_at=str(raw.get("ticket_created_at", "") or ""),
                )
        except Exception:
            pass
        time.sleep(poll_seconds)

    return sorted(seen.values(), key=lambda item: item.created_at)


def score_scenario(
    scenario: Scenario,
    started_at: datetime,
    incidents: list[IncidentSample],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scenario": scenario.name,
        "inject_fault": scenario.inject_fault,
        "expected_root_cause": scenario.expected_root_cause,
        "observed_incidents": len(incidents),
        "accuracy": 0.0,
        "false_positive": False,
        "missed_detection": False,
        "detected_expected_root_cause": False,
        "first_incident_latency_s": None,
        "ticket_latency_s": None,
        "best_incident": None,
        "root_cause_counts": dict(Counter(item.root_cause for item in incidents)),
        "mean_confidence": 0.0,
    }

    if not incidents:
        result["missed_detection"] = scenario.inject_fault
        return result

    best = max(incidents, key=lambda item: item.confidence)
    result["best_incident"] = {
        "incident_id": best.incident_id,
        "root_cause": best.root_cause,
        "confidence": round(best.confidence, 4),
        "created_at": best.created_at.isoformat(),
        "ticket_id": best.ticket_id,
        "ticket_status": best.ticket_status,
        "ticket_source": best.ticket_source,
        "ticket_created_at": best.ticket_created_at,
    }
    result["mean_confidence"] = round(sum(item.confidence for item in incidents) / len(incidents), 4)
    result["first_incident_latency_s"] = round((incidents[0].created_at - started_at).total_seconds(), 2)

    if scenario.expected_root_cause:
        match = next((item for item in incidents if item.root_cause == scenario.expected_root_cause), None)
        result["accuracy"] = 1.0 if match else 0.0
        result["detected_expected_root_cause"] = match is not None
        if match and match.ticket_created_at:
            try:
                result["ticket_latency_s"] = round(
                    (parse_iso(match.ticket_created_at) - match.created_at).total_seconds(), 2
                )
            except Exception:
                result["ticket_latency_s"] = None
        result["missed_detection"] = match is None
    else:
        result["false_positive"] = len(incidents) > 0

    return result


def run_suite(base_url: str, scenarios: list[Scenario], grace_seconds: int) -> list[dict[str, Any]]:
    suite: list[dict[str, Any]] = []

    for scenario in scenarios:
        print(f"\n=== Running scenario: {scenario.name} ===")
        started_at = utc_now()
        response = start_load(base_url, scenario)
        print(json.dumps(response, indent=2))

        wait_seconds = scenario.duration_seconds + grace_seconds
        print(f"Waiting {wait_seconds}s for incidents to appear...")
        incidents = collect_new_incidents(base_url, started_at, timeout_seconds=wait_seconds)
        scored = score_scenario(scenario, started_at, incidents)
        suite.append(scored)

        print(json.dumps(scored, indent=2))

    return suite


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    fault_runs = [item for item in results if item["inject_fault"]]
    baseline_runs = [item for item in results if not item["inject_fault"]]

    total_fault_runs = len(fault_runs)
    total_baseline_runs = len(baseline_runs)
    accuracy_rate = round(
        sum(item["accuracy"] for item in fault_runs) / total_fault_runs, 4
    ) if total_fault_runs else 0.0
    false_positive_rate = round(
        sum(1 for item in baseline_runs if item["false_positive"]) / total_baseline_runs, 4
    ) if total_baseline_runs else 0.0
    missed_detection_rate = round(
        sum(1 for item in fault_runs if item["missed_detection"]) / total_fault_runs, 4
    ) if total_fault_runs else 0.0

    all_latencies = [item["first_incident_latency_s"] for item in results if item["first_incident_latency_s"] is not None]
    all_ticket_latencies = [item["ticket_latency_s"] for item in results if item["ticket_latency_s"] is not None]
    all_confidences = [item["mean_confidence"] for item in results if item["mean_confidence"]]

    return {
        "accuracy_rate": accuracy_rate,
        "false_positive_rate": false_positive_rate,
        "missed_detection_rate": missed_detection_rate,
        "mean_detection_latency_s": round(sum(all_latencies) / len(all_latencies), 2) if all_latencies else None,
        "mean_ticket_latency_s": round(sum(all_ticket_latencies) / len(all_ticket_latencies), 2) if all_ticket_latencies else None,
        "mean_confidence": round(sum(all_confidences) / len(all_confidences), 4) if all_confidences else None,
        "fault_runs": total_fault_runs,
        "baseline_runs": total_baseline_runs,
    }


def build_scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.scenario == "baseline":
        return [
            Scenario(
                name="baseline",
                duration_seconds=args.duration,
                concurrency=args.concurrency,
                inject_fault=False,
                fault_db_latency_ms=50,
                fault_error_rate=0.0,
                fault_family="payment",
                expected_root_cause=None,
            )
        ]

    if args.scenario == "payment-latency":
        return [
            Scenario(
                name="payment-latency-baseline",
                duration_seconds=args.duration,
                concurrency=args.concurrency,
                inject_fault=False,
                fault_db_latency_ms=50,
                fault_error_rate=0.0,
                fault_family="payment",
                expected_root_cause=None,
            ),
            Scenario(
                name="payment-latency-moderate",
                duration_seconds=args.duration,
                concurrency=args.concurrency,
                inject_fault=True,
                fault_db_latency_ms=args.fault_latency,
                fault_error_rate=0.20,
                fault_family="payment",
                expected_root_cause=DEFAULT_EXPECTED_ROOT_CAUSE,
            ),
            Scenario(
                name="payment-latency-severe",
                duration_seconds=args.duration,
                concurrency=args.concurrency,
                inject_fault=True,
                fault_db_latency_ms=max(args.fault_latency, 800),
                fault_error_rate=0.30,
                fault_family="payment",
                expected_root_cause=DEFAULT_EXPECTED_ROOT_CAUSE,
            ),
        ]

    return [
        Scenario(
            name=args.scenario,
            duration_seconds=args.duration,
            concurrency=args.concurrency,
            inject_fault=args.inject_fault,
            fault_db_latency_ms=args.fault_latency,
            fault_error_rate=0.20 if args.inject_fault else 0.0,
            fault_family="payment",
            expected_root_cause=DEFAULT_EXPECTED_ROOT_CAUSE if args.inject_fault else None,
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="CausalIQ Phase 1 benchmark runner")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="Backend base URL")
    parser.add_argument("--scenario", default="payment-latency", help="baseline, payment-latency, or a custom scenario name")
    parser.add_argument("--duration", type=int, default=30, help="Scenario duration in seconds")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent request count")
    parser.add_argument("--fault-latency", type=int, default=700, help="Injected payment DB latency in ms")
    parser.add_argument("--inject-fault", action="store_true", help="Enable fault injection for custom scenario mode")
    parser.add_argument("--grace-seconds", type=int, default=90, help="Extra polling time after the load window")
    parser.add_argument("--output", default="", help="Optional path to write JSON results")
    args = parser.parse_args()

    try:
        wait_for_backend(args.backend)
        scenarios = build_scenarios(args)
        results = run_suite(args.backend, scenarios, grace_seconds=args.grace_seconds)
        summary = summarize(results)

        report = {
            "generated_at": utc_now().isoformat(),
            "backend": args.backend,
            "summary": summary,
            "results": results,
        }

        print("\n=== Phase 1 Summary ===")
        print(json.dumps(summary, indent=2))

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\nReport written to {output_path}")

        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())