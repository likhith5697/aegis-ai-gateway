"""Tests for the Grafana dashboard, datasource and alert rule (grafana/).

The query tests run every panel's Flux query, and the alert's, against the real
InfluxDB container, on a temporary bucket filled with known usage events.
"""

import os
import re
import time
import uuid
from pathlib import Path

import pytest
import yaml
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

import build_dashboard
import render_alerting
from consumer import to_point

GRAFANA = Path(build_dashboard.__file__).parent
DASHBOARD = build_dashboard.build()
INFLUX_ORG = os.environ.get("INFLUX_ORG", "aegis")
TENANTS = ["support", "engineering", "marketing"]


def panels_by_row() -> dict[str, list[str]]:
    rows, current = {}, None
    for p in DASHBOARD["panels"]:
        if p["type"] == "row":
            current = p["title"].split(":")[0]
            rows[current] = []
        else:
            rows[current].append(p["title"])
    return rows


# --- the dashboard as code ---

def test_committed_json_is_what_the_generator_produces():
    committed = (GRAFANA / "dashboards" / "aegis-gateway.json").read_text(encoding="utf-8")
    assert committed == build_dashboard.render(), "run: docker compose run --rm build-dashboard"


def test_dashboard_layout_matches_the_spec():
    assert (DASHBOARD["uid"], DASHBOARD["title"]) == ("aegis-gateway", "Aegis - AI Gateway")
    assert panels_by_row() == {
        "FinOps": ["Total spend", "Spend over time by tenant", "Spend by model", "Peak tokens per minute vs quota"],
        "Reliability": ["Requests per minute by tenant", "Error rate by status (%)", "Failovers over time",
                        "Latency p50 / p95 by model"],
        "Security": ["PII redactions over time by tenant", "Rate-limited requests (429) by tenant"],
    }


def test_tenant_dropdown_offers_all_and_each_tenant():
    tenant = next(v for v in DASHBOARD["templating"]["list"] if v["name"] == "tenant")
    assert tenant["query"].split(",") == TENANTS
    assert tenant["includeAll"] and tenant["current"]["value"] == ["$__all"]


def test_each_tenant_has_one_color_in_every_panel():
    colors_seen: dict[str, set[str]] = {}
    for p in DASHBOARD["panels"]:
        for override in p.get("fieldConfig", {}).get("overrides", []):
            name = override["matcher"]["options"]
            if name in build_dashboard.TENANT_COLORS:
                colors_seen.setdefault(name, set()).add(override["properties"][0]["value"]["fixedColor"])
    assert set(colors_seen) >= set(TENANTS)
    assert all(len(colors) == 1 for colors in colors_seen.values())


def test_every_query_uses_the_provisioned_datasource():
    datasource = yaml.safe_load((GRAFANA / "provisioning" / "datasources" / "influxdb.yaml").read_text())["datasources"][0]
    assert datasource["secureJsonData"]["token"] == "$INFLUXDB_ADMIN_TOKEN"  # from the environment, not the file
    for p in DASHBOARD["panels"]:
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == datasource["uid"]


def test_grafana_requires_login():
    compose = yaml.safe_load((GRAFANA.parent / "docker-compose.yml").read_text())
    env = compose["services"]["grafana"]["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert env["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")


def alert_rule(threshold: str = "0.05") -> dict:
    return yaml.safe_load(render_alerting.render(threshold))["groups"][0]["rules"][0]


def test_alert_threshold_comes_from_the_environment():
    rule = alert_rule("12.5")
    assert rule["data"][2]["model"]["conditions"][0]["evaluator"]["params"] == [12.5]  # a real number
    assert "over the 12.5 USD threshold" in rule["annotations"]["summary"]
    assert "{{ $labels.tenant }}" in rule["annotations"]["summary"]  # Grafana's own template, untouched


@pytest.mark.parametrize("bad", ["", "lots", "-1", "0"])
def test_bad_threshold_stops_grafana_from_starting_with_a_broken_rule(bad):
    with pytest.raises(ValueError, match="AEGIS_SPEND_ALERT_USD"):
        render_alerting.render(bad)


# --- every query runs on the real InfluxDB ---

@pytest.fixture(scope="module")
def influx():
    client = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=INFLUX_ORG)
    yield client
    client.close()


def event(minutes_ago, tenant, status=200, cost=0.001, tokens=(100, 40), fallback=False, pii=0, rate_limited=False):
    used = "none" if status != 200 else ("claude-sonnet-5-5" if fallback else "claude-opus-5-5")
    return {
        "timestamp_ns": time.time_ns() - int(minutes_ago * 60e9), "request_id": uuid.uuid4().hex,
        "gateway": "test", "tenant": tenant, "requested_model": "claude-opus-5-5", "model_used": used,
        "fallback": fallback, "prompt_tokens": tokens[0] if status == 200 else 0,
        "completion_tokens": tokens[1] if status == 200 else 0, "cost_usd": cost if status == 200 else 0.0,
        "latency_ms": 120.0, "status_code": status, "pii_redactions": pii, "rate_limited": rate_limited,
    }


SEED = [
    event(9, "support", cost=0.002, pii=1),
    event(8, "support", cost=0.003),
    event(7, "engineering", cost=0.010, fallback=True),
    event(6, "engineering", cost=0.020, tokens=(9000, 1000)),
    event(5, "marketing", cost=0.001, pii=2),
    event(4, "marketing", status=429, rate_limited=True),
    event(4, "marketing", status=429, rate_limited=True),
    event(3, "support", status=503),
    event(2, "unknown", status=400),
]
SPEND = {"support": 0.005, "engineering": 0.030, "marketing": 0.001}


@pytest.fixture(scope="module")
def bucket(influx):
    created = influx.buckets_api().create_bucket(bucket_name=f"aegis_test_{uuid.uuid4().hex[:8]}", org=INFLUX_ORG)
    influx.write_api(write_options=SYNCHRONOUS).write(bucket=created.name, record=[to_point(e) for e in SEED])
    yield created.name
    influx.buckets_api().delete_bucket(created)


def run(influx, bucket, query: str) -> list[dict]:
    """Run a query the way Grafana does: fill in the variables and the v record it provides."""
    query = (query.replace('from(bucket: "usage")', f'from(bucket: "{bucket}")')
             .replace("${tenant:regex}", ".*").replace("${gateway:regex}", ".*"))
    lines = query.split("\n")
    imports = [line for line in lines if line.startswith("import ")]
    rest = [line for line in lines if not line.startswith("import ")]
    v = "v = {timeRangeStart: -1h, timeRangeStop: now(), windowPeriod: 1m}"
    tables = influx.query_api().query("\n".join([*imports, v, *rest]), org=INFLUX_ORG)
    return [record.values for table in tables for record in table.records]


PANEL_QUERIES = [(p["title"], t["refId"], t["query"]) for p in DASHBOARD["panels"] for t in p.get("targets", [])]


@pytest.mark.parametrize("title, ref, query", PANEL_QUERIES, ids=[f"{t}-{r}" for t, r, _ in PANEL_QUERIES])
def test_every_panel_query_runs_and_returns_data(influx, bucket, title, ref, query):
    assert run(influx, bucket, query), f"no data for panel {title!r}"


def panel_query(title: str) -> str:
    return next(q for t, _, q in PANEL_QUERIES if t == title)


def test_total_spend_adds_up(influx, bucket):
    [row] = run(influx, bucket, panel_query("Total spend"))
    assert row["_value"] == pytest.approx(sum(SPEND.values()))


def test_rate_limited_counts_by_tenant(influx, bucket):
    [row] = run(influx, bucket, panel_query("Rate-limited requests (429) by tenant"))
    assert row["marketing"] == 2 and "support" not in row


def test_quota_gauge_uses_tenants_yaml(influx, bucket):
    query = panel_query("Peak tokens per minute vs quota")
    tenants = yaml.safe_load((GRAFANA.parent / "gateway" / "tenants.yaml").read_text())["tenants"]
    for name, settings in tenants.items():
        assert f'"{name}": {float(settings["tpm_quota"])}' in query
    [row] = run(influx, bucket, query)
    assert row["engineering"] == pytest.approx(10000 / 100000 * 100)  # its busiest minute: 10,000 tokens


def test_error_rate_shows_429_4xx_and_5xx(influx, bucket):
    rows = run(influx, bucket, panel_query("Error rate by status (%)"))
    assert max(r["429"] for r in rows) > 0
    assert max(r["4xx"] for r in rows) > 0
    assert max(r["5xx"] for r in rows) > 0


def test_alert_query_sums_spend_per_tenant(influx, bucket):
    query = alert_rule()["data"][0]["model"]["query"]
    rows = run(influx, bucket, query)
    assert {r["tenant"]: round(r["_value"], 6) for r in rows if r["tenant"] in SPEND} == SPEND
