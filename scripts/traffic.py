"""Send a realistic mix of traffic through the sandbox gateway, for the usage dashboard.

    docker compose run --rm traffic                 # about 90 seconds
    docker compose run --rm traffic --duration 300  # longer, for a fuller dashboard

All three tenants, mixed prompt sizes, some PII, plus four scenarios:
  - a marketing burst that overflows its 20,000 tokens/minute budget (429s)
  - a window where the primary model returns 503s, so engineering's Opus requests fail over
  - a few bad requests: a model the tenant may not use (403), a missing Tenant-ID (400)
  - a short full provider outage: every model returns 503, so requests fail (5xx), and right
    after it the circuit breaker briefly skips both models (router 429s, not our rate limit)
Only the sandbox's chaos models are called: no real model, no cost.
"""

import argparse
import asyncio
import os
import random
import sys
import time
from collections import Counter, defaultdict

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://litellm-sandbox:4000")
CHAOS_URL = os.environ.get("CHAOS_URL", "http://chaos:8080")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"

PROMPTS = [
    "Summarize our refund policy in two sentences.",
    "Write a haiku about on-call rotations.",
    "Explain the difference between a mutex and a semaphore with an example.",
    "Draft a polite reply to a customer whose order arrived late.",
    "List five ideas for a spring marketing campaign for a coffee brand.",
    "Review this function for bugs: def add(a, b): return a - b",
    "Translate 'thank you for your patience' into Spanish, French and German.",
]
PII_PROMPTS = [  # scrubbed by the gateway; the events only count them
    "Customer jane.doe@example.com says her card 4111 1111 1111 1111 was charged twice.",
    "Call me back on 555-123-4567 about ticket 8812.",
    "My SSN is 123-45-6789, why was my application rejected?",
]
# tenant: (share of traffic, models it uses)
TENANTS = {"support": (0.5, [SONNET]), "engineering": (0.35, [OPUS, OPUS, SONNET]), "marketing": (0.15, [SONNET])}

results: dict[str, Counter] = defaultdict(Counter)


async def send(client, tenant, model, max_tokens, prompt=None, label=None):
    headers = {"Authorization": f"Bearer {MASTER_KEY}"}
    if tenant:
        headers["Tenant-ID"] = tenant
    if prompt is None:
        prompt = random.choice(PII_PROMPTS) if random.random() < 0.25 else random.choice(PROMPTS)
    r = await client.post("/v1/chat/completions", headers=headers, json={
        "model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}],
    })
    key = label or tenant
    results[key][r.status_code] += 1
    if r.headers.get("x-aegis-fallback") == "true":
        results[key]["fallback"] += 1


async def background(client, stop):
    """The everyday traffic: one request every 0.2-0.6s from a random tenant."""
    names, weights = list(TENANTS), [share for share, _ in TENANTS.values()]
    while not stop.is_set():
        tenant = random.choices(names, weights)[0]
        model = random.choice(TENANTS[tenant][1])
        await send(client, tenant, model, max_tokens=random.choice([50, 150, 300, 600]))
        await asyncio.sleep(random.uniform(0.2, 0.6))


async def marketing_burst(client):
    print("  scenario: marketing burst (6 x 9,000 tokens at once against a 20,000/min budget)")
    await asyncio.gather(*(send(client, "marketing", SONNET, max_tokens=9000) for _ in range(6)))


async def forced_fallback(client):
    print("  scenario: primary model returns 503 for 8s; engineering's Opus requests fail over")
    await client.post(f"{CHAOS_URL}/chaos", params={"mode": "503"})
    for _ in range(4):
        await send(client, "engineering", OPUS, max_tokens=200)
        await asyncio.sleep(2)
    await client.post(f"{CHAOS_URL}/chaos", params={"mode": "ok"})


async def bad_requests(client):
    print("  scenario: bad requests (marketing asks for Opus: 403; no Tenant-ID: 400)")
    await send(client, "marketing", OPUS, max_tokens=100)
    await send(client, "marketing", OPUS, max_tokens=100)
    await send(client, None, SONNET, max_tokens=100, label="(no tenant)")


async def provider_outage(client):
    print("  scenario: full provider outage for 4s (every model returns 503)")
    await client.post(f"{CHAOS_URL}/chaos", params={"mode": "503", "target": "all"})
    await asyncio.sleep(4)  # the background traffic keeps going and hits the outage
    await client.post(f"{CHAOS_URL}/chaos", params={"mode": "ok"})


async def wait_for_gateway(client):
    for _ in range(90):
        try:
            if (await client.get("/health/liveliness")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    sys.exit(f"Gateway at {GATEWAY_URL} did not become ready")


async def main(args) -> None:
    if not MASTER_KEY:
        sys.exit("LITELLM_MASTER_KEY is not set (compose passes it from .env)")
    random.seed(args.seed)
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=60) as client:
        await wait_for_gateway(client)
        await client.post(f"{CHAOS_URL}/chaos", params={"mode": "ok"})
        print(f"Sending mixed traffic to {GATEWAY_URL} for {args.duration}s")
        stop = asyncio.Event()
        traffic = asyncio.create_task(background(client, stop))
        scenarios = [(0.2, marketing_burst), (0.45, forced_fallback), (0.7, bad_requests), (0.85, provider_outage)]
        started = time.monotonic()
        for at, scenario in scenarios:
            await asyncio.sleep(max(0, started + at * args.duration - time.monotonic()))
            await scenario(client)
        await asyncio.sleep(max(0, started + args.duration - time.monotonic()))
        stop.set()
        await traffic

    print("\nRequests by tenant (HTTP status counts, and how many were answered by a fallback)")
    for name in sorted(results):
        counts = results[name]
        statuses = "  ".join(f"{code}={n}" for code, n in sorted(counts.items(), key=str) if code != "fallback")
        print(f"  {name:<12} {statuses}  fallback={counts['fallback']}")
    print("\nEvents reach InfluxDB within a few seconds. Check with:")
    print('  docker compose exec influxdb influx query \'from(bucket:"usage") |> range(start:-1h) '
          '|> filter(fn:(r) => r._field == "cost_usd") |> group(columns:["tenant"]) |> sum()\'')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=90, help="seconds of traffic (default 90)")
    parser.add_argument("--seed", type=int, default=7, help="random seed, for repeatable traffic")
    asyncio.run(main(parser.parse_args()))
