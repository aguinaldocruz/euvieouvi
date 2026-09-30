#!/usr/bin/env python3
"""Read-only load/soak check for euvieouvi operational endpoints."""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_ENDPOINTS = (
    "/health/ready",
    "/health/metrics",
    "/jobs/statuses",
    "/jobs/events?once=1",
)


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, latency_ms: float, status: str, error: str | None = None) -> None:
        with self.lock:
            self.latencies_ms.append(latency_ms)
            self.statuses[status] += 1
            if error and len(self.errors) < 20:
                self.errors.append(error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run concurrent read-only requests and enforce latency/error thresholds."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=30.0, help="Seconds to run.")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    parser.add_argument("--endpoint", action="append", dest="endpoints")
    args = parser.parse_args()
    if args.duration <= 0 or not 1 <= args.concurrency <= 64 or args.timeout <= 0:
        parser.error("duration/timeout must be positive and concurrency must be from 1 to 64")
    if not 0 <= args.max_error_rate <= 1 or args.max_p95_ms <= 0:
        parser.error("thresholds are invalid")
    return args


def run_worker(
    worker_id: int,
    *,
    base_url: str,
    endpoints: tuple[str, ...],
    deadline: float,
    timeout: float,
    results: Results,
) -> None:
    index = worker_id
    while time.monotonic() < deadline:
        endpoint = endpoints[index % len(endpoints)]
        index += 1
        started = time.perf_counter()
        try:
            request = Request(
                urljoin(f"{base_url.rstrip('/')}/", endpoint.lstrip("/")),
                headers={"Accept": "application/json", "User-Agent": "euvieouvi-load-check/1"},
            )
            with urlopen(request, timeout=timeout) as response:
                response.read()
                status = str(response.status)
                error = None if 200 <= response.status < 400 else f"{endpoint}: HTTP {status}"
        except HTTPError as exc:
            status = str(exc.code)
            error = f"{endpoint}: HTTP {exc.code}"
        except (TimeoutError, URLError, OSError) as exc:
            status = "transport_error"
            error = f"{endpoint}: {type(exc).__name__}: {exc}"
        results.add((time.perf_counter() - started) * 1000, status, error)


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percent) - 1))
    return ordered[index]


def main() -> int:
    args = parse_args()
    endpoints = tuple(args.endpoints or DEFAULT_ENDPOINTS)
    results = Results()
    started = time.monotonic()
    deadline = started + args.duration
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                run_worker,
                worker,
                base_url=args.base_url,
                endpoints=endpoints,
                deadline=deadline,
                timeout=args.timeout,
                results=results,
            )
            for worker in range(args.concurrency)
        ]
        for future in futures:
            future.result()

    total = len(results.latencies_ms)
    failures = total - results.statuses.get("200", 0)
    error_rate = failures / total if total else 1.0
    report = {
        "duration_seconds": round(time.monotonic() - started, 3),
        "concurrency": args.concurrency,
        "requests": total,
        "requests_per_second": round(total / max(args.duration, 0.001), 2),
        "statuses": dict(sorted(results.statuses.items())),
        "error_rate": round(error_rate, 5),
        "latency_ms": {
            "p50": round(percentile(results.latencies_ms, 0.50), 2),
            "p95": round(percentile(results.latencies_ms, 0.95), 2),
            "p99": round(percentile(results.latencies_ms, 0.99), 2),
            "max": round(max(results.latencies_ms, default=0.0), 2),
        },
        "sample_errors": results.errors,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(
        total == 0
        or error_rate > args.max_error_rate
        or report["latency_ms"]["p95"] > args.max_p95_ms
    )


if __name__ == "__main__":
    raise SystemExit(main())
