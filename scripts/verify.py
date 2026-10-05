"""One-shot verification service.

Runs after the API container is healthy (see docker-compose.yml) and:

1. runs the code test suite,
2. performs a build sanity check (byte-compilation of every module),
3. exercises the HTTP API with a consistent chain (including two paths
   that must agree), a redundant single_relation chain, a chain that fails
   resilience (422 with sorted criticalRelationIds and no coefficients),
   parallel relations counted as independent evidence, a contradictory
   cycle, an unreachable pair and an invalid relation,

then prints a summary and exits non-zero if anything failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
HEALTH_URL = f"{BASE_URL}/health"
TRANSLATE_URL = f"{BASE_URL}/api/calibrations/translate"

failures: list[str] = []


def wait_for_healthy(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:  # noqa: BLE001 - retry any startup error
            last_error = exc
        time.sleep(0.5)
    print(f"[wait] service never became healthy: {last_error}", file=sys.stderr)
    return False


def run_step(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name}: {' '.join(cmd)} ===", flush=True)
    proc = subprocess.run(cmd, cwd="/app" if os.path.isdir("/app") else ".")
    ok = proc.returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} (exit {proc.returncode}) ---")
    if not ok:
        failures.append(name)
    return ok


def post(payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        TRANSLATE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[smoke] {name}: {'PASS' if condition else 'FAIL'} {detail}".rstrip())
    if not condition:
        failures.append(name)


def smoke() -> None:
    print("\n=== API smoke tests ===", flush=True)

    consistent = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "readings": ["0", "1", "5/2"],
    }
    status, body = post(consistent)
    check(
        "consistent chain returns 200 and exact transform",
        status == 200
        and body.get("coefficients") == {"a": "1", "b": "5/6"}
        and body.get("results") == ["5/6", "11/6", "10/3"],
        f"status={status} body={body}",
    )
    check(
        "legacy contract omits the resilience field",
        "resilience" not in body,
        f"body={body}",
    )

    diamond = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "A", "to": "D", "a": "4", "b": "2"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
            {"from": "D", "to": "C", "a": "1/4", "b": "1/3"},
        ],
        "readings": ["2"],
    }
    status, body = post(diamond)
    check(
        "two agreeing paths both resolve to x + 5/6",
        status == 200
        and body.get("coefficients") == {"a": "1", "b": "5/6"}
        and body.get("results") == ["17/6"],
        f"status={status} body={body}",
    )

    resilient_diamond = {
        "source": "A",
        "target": "C",
        "resilience": "single_relation",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1", "relationId": "ab"},
            {"from": "A", "to": "D", "a": "4", "b": "2", "relationId": "ad"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3", "relationId": "bc"},
            {"from": "D", "to": "C", "a": "1/4", "b": "1/3", "relationId": "dc"},
        ],
        "readings": ["2"],
    }
    status, body = post(resilient_diamond)
    check(
        "redundant diamond survives every single-relation revocation",
        status == 200
        and body.get("coefficients") == {"a": "1", "b": "5/6"}
        and body.get("results") == ["17/6"]
        and body.get("resilience") == {"mode": "single_relation", "verified": True},
        f"status={status} body={body}",
    )

    fragile = {
        "source": "A",
        "target": "C",
        "resilience": "single_relation",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1", "relationId": "r1"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3", "relationId": "r2"},
        ],
        "readings": ["2"],
    }
    status, body = post(fragile)
    error = body.get("error", {})
    check(
        "single chain fails resilience: 422, sorted ids, no coefficients",
        status == 422
        and error.get("code") == "resilience_not_met"
        and error.get("criticalRelationIds") == ["r1", "r2"]
        and "coefficients" not in body
        and "results" not in body,
        f"status={status} body={body}",
    )

    parallel = {
        "source": "A",
        "target": "C",
        "resilience": "single_relation",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1", "relationId": "p1"},
            {"from": "A", "to": "B", "a": "2", "b": "1", "relationId": "p2"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3", "relationId": "p3"},
        ],
        "readings": ["2"],
    }
    status, body = post(parallel)
    error = body.get("error", {})
    check(
        "parallel relations count independently; lone tail link is critical",
        status == 422
        and error.get("criticalRelationIds") == ["p3"],
        f"status={status} body={body}",
    )

    resilient_conflict = {
        "source": "A",
        "target": "C",
        "resilience": "single_relation",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1", "relationId": "r1"},
            {"from": "B", "to": "C", "a": "3", "b": "0", "relationId": "r2"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0", "relationId": "r3"},
        ],
        "readings": ["1"],
    }
    status, body = post(resilient_conflict)
    check(
        "contradiction beats resilience with original 409 verdict",
        status == 409 and body.get("error", {}).get("code") == "conflict",
        f"status={status} body={body}",
    )

    missing_ids = dict(fragile)
    missing_ids["relations"] = [
        {"from": "A", "to": "B", "a": "2", "b": "1"},
        {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
    ]
    status, body = post(missing_ids)
    check(
        "single_relation mode requires unique non-empty relationId",
        status == 400 and body.get("error", {}).get("code") == "invalid_request",
        f"status={status} body={body}",
    )

    contradictory = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0"},
        ],
        "readings": ["1"],
    }
    status, body = post(contradictory)
    error = body.get("error", {})
    check(
        "contradictory cycle is rejected (409 conflict, no results)",
        status == 409
        and error.get("code") == "conflict"
        and set(error.get("cycle", [])) == {"A", "B", "C"}
        and "results" not in body,
        f"status={status} body={body}",
    )

    disconnected = {
        "source": "A",
        "target": "E",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "D", "to": "E", "a": "1", "b": "0"},
        ],
        "readings": ["1"],
    }
    status, body = post(disconnected)
    error = body.get("error", {})
    check(
        "unreachable pair returns recognizable error",
        status == 404 and error.get("code") == "unreachable"
        and error.get("source") == "A" and error.get("target") == "E",
        f"status={status} body={body}",
    )

    invalid = {
        "source": "A",
        "target": "B",
        "relations": [{"from": "A", "to": "B", "a": "0", "b": "0"}],
        "readings": ["1"],
    }
    status, body = post(invalid)
    check(
        "zero multiplier is rejected",
        status == 400 and body.get("error", {}).get("code") == "invalid_request",
        f"status={status} body={body}",
    )


def main() -> int:
    if not wait_for_healthy():
        return 1
    run_step("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    run_step("build sanity (compileall)", [sys.executable, "-m", "compileall", "-q", "app", "tests"])
    smoke()

    print("\n=== verification summary ===")
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}", file=sys.stderr)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
