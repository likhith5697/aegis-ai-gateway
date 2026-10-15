"""Failover demo: send requests in a loop and show which model answered each one.

Runs against the sandbox gateway, where claude-opus-5-5 is the breakable chaos
model and claude-sonnet-5-5 its always-healthy fallback. No real model, no cost.

    docker compose run --rm failover-demo              # loop; flip the chaos model from another terminal
    docker compose run --rm failover-demo --set 503    # flip: ok | 429 | 503 | hang | 401 | 400 | cut
    docker compose run --rm failover-demo --auto       # scripted tour of every case, ends with PASS/FAIL
"""

import argparse
import os
import sys
import time

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://litellm-sandbox:4000")
CHAOS_URL = os.environ.get("CHAOS_URL", "http://chaos:8080")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
TENANT = "engineering"          # allowed to use both models, so it may fail over
PRIMARY = "claude-opus-5-5"     # the chaos model in the sandbox
FALLBACK = "claude-sonnet-5-5"


def chaos(client: httpx.Client, mode: str | None = None) -> dict:
    if mode is None:
        return client.get(f"{CHAOS_URL}/chaos").json()
    return client.post(f"{CHAOS_URL}/chaos", params={"mode": mode}).json()


def send(client: httpx.Client) -> dict:
    started = time.monotonic()
    r = client.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {MASTER_KEY}", "Tenant-ID": TENANT},
        json={"model": PRIMARY, "max_tokens": 20, "messages": [{"role": "user", "content": "Say hi."}]},
        timeout=60,
    )
    result = {
        "status": r.status_code,
        "model": r.headers.get("x-aegis-model-used", "-"),
        "fallback": r.headers.get("x-aegis-fallback", "-"),
        "ms": (time.monotonic() - started) * 1000,
    }
    if r.status_code != 200:
        result["error"] = r.json().get("error", {}).get("message", r.text)[:70]
    return result


def show(result: dict, mode: str, primary_hits: int) -> None:
    line = f"  {time.strftime('%H:%M:%S')}  chaos={mode:<4}  HTTP {result['status']}"
    if result["status"] == 200:
        line += f"  model={result['model']:<18} fallback={result['fallback']:<5}"
    else:
        line += f"  {result['error']}"
    print(f"{line}  {result['ms']:6.0f} ms  primary called={primary_hits}", flush=True)


def wait_for_gateway(client: httpx.Client) -> None:
    for _ in range(90):
        try:
            if client.get(f"{GATEWAY_URL}/health/liveliness").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit(f"Gateway at {GATEWAY_URL} did not become ready")


def loop(client: httpx.Client, interval: float) -> None:
    print(f"Sending a request every {interval}s as tenant '{TENANT}' to {PRIMARY}. Ctrl+C to stop.")
    print("Flip the chaos model from another terminal:  docker compose run --rm failover-demo --set 503\n")
    while True:
        state = chaos(client)
        result = send(client)
        show(result, state["mode"], chaos(client)["primary_requests"])
        time.sleep(interval)


def run_phase(client, mode, requests=None, until_primary=False, max_seconds=30.0):
    """Switch the chaos model to `mode`, then send `requests` requests (or until the primary answers)."""
    print(f"\n--- chaos model -> {mode}" + (" (waiting for the primary to recover)" if until_primary else ""))
    chaos(client, mode)
    results, started = [], time.monotonic()
    while True:
        result = send(client)
        hits = chaos(client)["primary_requests"]
        show(result, mode, hits)
        results.append(result)
        if until_primary and result.get("model") == PRIMARY:
            break
        if not until_primary and len(results) >= requests:
            break
        if time.monotonic() - started > max_seconds:
            break
        time.sleep(1)
    return results, hits


def auto(client: httpx.Client) -> int:
    print(f"Scripted failover tour as tenant '{TENANT}': {PRIMARY} (chaos) with fallback {FALLBACK}")
    checks = []

    ok, _ = run_phase(client, "ok", requests=3)
    checks.append((all(r["model"] == PRIMARY and r["fallback"] == "false" for r in ok),
                   "healthy primary answers itself, no fallback"))

    bad_key, _ = run_phase(client, "401", requests=2)
    checks.append((all(r["status"] == 401 for r in bad_key), "401 is returned as-is, no fallback"))

    for mode, requests in (("503", 4), ("hang", 3), ("429", 3)):
        failed, hits = run_phase(client, mode, requests=requests)
        checks.append((all(r["status"] == 200 and r["model"] == FALLBACK for r in failed),
                       f"{mode}: every request answered by the fallback"))
        # Without a circuit breaker the primary is tried on every request (503 twice: 1 retry).
        tries_per_request = 2 if mode == "503" else 1
        checks.append((hits < requests * tries_per_request,
                       f"{mode}: circuit breaker skipped the primary ({hits} calls for {requests} requests)"))
        recovered, _ = run_phase(client, "ok", until_primary=True)
        checks.append((recovered[-1]["model"] == PRIMARY, f"after {mode}: primary back once its cooldown ended"))

    print("\nResult")
    for passed, text in checks:
        print(f"  {'PASS' if passed else 'FAIL'}: {text}")
    return 0 if all(p for p, _ in checks) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", metavar="MODE", help="switch the chaos model: ok, 429, 503, hang, 401, 400, cut")
    parser.add_argument("--auto", action="store_true", help="run the scripted tour and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between requests in loop mode")
    args = parser.parse_args()

    with httpx.Client() as client:
        if args.set:
            print("chaos model is now:", chaos(client, args.set))
            return 0
        if not MASTER_KEY:
            sys.exit("LITELLM_MASTER_KEY is not set (compose passes it from .env)")
        wait_for_gateway(client)
        if args.auto:
            return auto(client)
        try:
            loop(client, args.interval)
        except KeyboardInterrupt:
            chaos(client, "ok")
            print("\nstopped; chaos model reset to ok")
        return 0


if __name__ == "__main__":
    sys.exit(main())
