"""Aegis load test: hammer one tenant until it gets 429s while another keeps succeeding.

Run in Docker:
    docker compose run --rm loadtest

Compose points it at "litellm-sandbox": a second gateway with the same hooks,
tenants and Redis, whose models are the chaos server in scripts/chaos_llm.py.
Every request goes through the real tenant check, PII scrub and token budget;
only the model is fake, so it costs nothing.
"""

import argparse
import asyncio
import os
import sys
import time
from collections import Counter

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://litellm:4000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
MODEL = "claude-sonnet-5-5"  # allowed for both default tenants


class TenantStats:
    def __init__(self, name: str):
        self.name = name
        self.status = Counter()
        self.limit = "?"
        self.remaining = "?"
        self.first_429_at: float | None = None
        self.retry_after = "?"

    def line(self) -> str:
        other = sum(n for code, n in self.status.items() if code not in (200, 429))
        return (f"{self.name:<12} sent={sum(self.status.values()):<5} 200={self.status[200]:<5} "
                f"429={self.status[429]:<5} other={other:<3} limit={self.limit} remaining={self.remaining}")


async def send(client: httpx.AsyncClient, stats: TenantStats, args, started: float) -> None:
    body = {
        "model": MODEL,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": "Reply with one word."}],
    }
    try:
        r = await client.post("/v1/chat/completions", json=body, headers={"Tenant-ID": stats.name})
    except httpx.HTTPError as e:
        stats.status[type(e).__name__] += 1
        return
    stats.status[r.status_code] += 1
    stats.limit = r.headers.get("x-aegis-ratelimit-limit", stats.limit)
    if r.status_code == 200:
        stats.remaining = r.headers.get("x-aegis-ratelimit-remaining", stats.remaining)
    elif r.status_code == 429:
        stats.remaining = r.headers.get("x-aegis-ratelimit-remaining", "0")
        stats.retry_after = r.headers.get("retry-after", "?")
        if stats.first_429_at is None:
            stats.first_429_at = time.monotonic() - started
            print(f"  first 429 for {stats.name} after {stats.first_429_at:.1f}s: {r.json()['error']['message']}")


async def hammer(client, stats, args, started, stop: asyncio.Event) -> None:
    """As fast as possible, from many workers at once. Ignores Retry-After on purpose."""
    async def worker():
        while not stop.is_set():
            await send(client, stats, args, started)
    await asyncio.gather(*(worker() for _ in range(args.concurrency)))


async def steady(client, stats, args, started, stop: asyncio.Event) -> None:
    """A well-behaved tenant: a few requests per second."""
    while not stop.is_set():
        await send(client, stats, args, started)
        await asyncio.sleep(1 / args.steady_rps)


async def wait_for_gateway(client: httpx.AsyncClient) -> None:
    for _ in range(90):
        try:
            if (await client.get("/health/liveliness")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    sys.exit(f"Gateway at {GATEWAY_URL} did not become ready")


async def main(args) -> int:
    if not MASTER_KEY:
        sys.exit("LITELLM_MASTER_KEY is not set (compose passes it from .env)")

    noisy, quiet = TenantStats(args.hammer), TenantStats(args.steady)
    async with httpx.AsyncClient(
        base_url=GATEWAY_URL,
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        timeout=60,
        limits=httpx.Limits(max_connections=args.concurrency + 5),
    ) as client:
        await wait_for_gateway(client)
        print(f"Aegis load test against {GATEWAY_URL}")
        print(f"  hammer: {args.hammer} with {args.concurrency} parallel workers")
        print(f"  steady: {args.steady} at {args.steady_rps} req/s")
        print(f"  stops after {args.duration}s, or once {args.hammer} has {args.stop_after} 429s\n")

        started = time.monotonic()
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(hammer(client, noisy, args, started, stop)),
            asyncio.create_task(steady(client, quiet, args, started, stop)),
        ]
        while time.monotonic() - started < args.duration and noisy.status[429] < args.stop_after:
            await asyncio.sleep(1)
            print(f"  t={time.monotonic() - started:4.0f}s  {noisy.line()}")
            print(f"           {quiet.line()}")
        stop.set()
        await asyncio.gather(*tasks)

    print("\nResult")
    print(f"  {noisy.line()}  retry-after={noisy.retry_after}s")
    print(f"  {quiet.line()}")

    noisy_other = sum(n for code, n in noisy.status.items() if code not in (200, 429))
    quiet_failed = sum(n for code, n in quiet.status.items() if code != 200)
    checks = [
        (noisy.status[429] > 0, f"{args.hammer} was rate-limited (429)"),
        (noisy_other == 0, f"{args.hammer} saw no errors other than 429"),
        (quiet.status[200] > 0 and quiet_failed == 0, f"{args.steady} kept succeeding (every request 200)"),
    ]
    for ok, text in checks:
        print(f"  {'PASS' if ok else 'FAIL'}: {text}")
    return 0 if all(ok for ok, _ in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hammer", default="marketing", help="tenant to overload (default: marketing)")
    parser.add_argument("--steady", default="engineering", help="tenant that should keep working")
    parser.add_argument("--concurrency", type=int, default=20, help="parallel workers for the hammered tenant")
    parser.add_argument("--steady-rps", type=float, default=2, help="requests per second for the steady tenant")
    parser.add_argument("--max-tokens", type=int, default=200, help="max_tokens per request")
    parser.add_argument("--duration", type=int, default=60, help="give up after this many seconds")
    parser.add_argument("--stop-after", type=int, default=200, help="stop once the hammered tenant has this many 429s")
    sys.exit(asyncio.run(main(parser.parse_args())))
