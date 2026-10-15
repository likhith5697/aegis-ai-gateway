"""Builds the Grafana dashboard: grafana/dashboards/aegis-gateway.json.

Grafana loads that JSON file at startup (provisioning). This script is the
readable source of it: one function call per panel, with its Flux query. The
tenant list and the tpm_quota values come from gateway/tenants.yaml, so the
dashboard can't drift from the real config.

After changing a panel or tenants.yaml, regenerate and commit the JSON:
    docker compose run --rm build-dashboard
A test fails if the committed JSON is not what this script produces.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
TENANTS_FILE = ROOT.parent / "gateway" / "tenants.yaml"
OUTPUT = ROOT / "dashboards" / "aegis-gateway.json"

DATASOURCE = {"type": "influxdb", "uid": "aegis-influxdb"}  # from provisioning/datasources/influxdb.yaml
# One color per tenant, used by every panel that splits by tenant.
TENANT_COLORS = {"support": "#5794F2", "engineering": "#B877D9", "marketing": "#FF9830", "unknown": "#8E8E8E"}
ERROR_COLORS = {"429": "#FADE2A", "4xx": "#FF9830", "5xx": "#F2495C"}


# --- Flux building blocks ---

def source(field: str, *conditions: str) -> list[str]:
    """Rows of one field in the selected time range, tenants and gateways (the dropdowns)."""
    lines = [
        'from(bucket: "usage")',
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)",
        f'  |> filter(fn: (r) => r._measurement == "llm_usage" and r._field == "{field}")',
        "  |> filter(fn: (r) => r.tenant =~ /^${tenant:regex}$/ and r.gateway =~ /^${gateway:regex}$/)",
    ]
    return lines + [f"  |> filter(fn: (r) => {c})" for c in conditions]


def one_column_per(column: str, row_key: str = "_time") -> list[str]:
    """Turn one table per tenant (or model, ...) into one table with a column each.
    Then every series is named plainly "support", "engineering", ... and colors can match by name."""
    return [
        f'  |> keep(columns: ["{row_key}", "_value", "{column}"])',
        "  |> group()",
        f'  |> pivot(rowKey: ["{row_key}"], columnKey: ["{column}"], valueColumn: "_value")',
    ]


def per_tenant_over_time(fn: str, fill: str | None = None) -> list[str]:
    lines = [
        '  |> group(columns: ["tenant"])',
        f"  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: true)",
    ]
    if fill is not None:  # an empty window sums to null; show it as 0 instead of a gap
        lines.append(f"  |> fill(value: {fill})")
    return lines + one_column_per("tenant")


def total_per_tenant() -> list[str]:
    """One row, one column per tenant (for bar gauges)."""
    return [
        '  |> map(fn: (r) => ({row: "total", tenant: r.tenant, _value: r._value}))',
        "  |> group()",
        '  |> pivot(rowKey: ["row"], columnKey: ["tenant"], valueColumn: "_value")',
        '  |> drop(columns: ["row"])',
    ]


def flux(*lines: str, imports: tuple[str, ...] = ()) -> str:
    return "\n".join([*(f'import "{name}"' for name in imports), *lines])


# --- panel building blocks ---

def color_overrides(colors: dict[str, str]) -> list[dict]:
    return [
        {"matcher": {"id": "byName", "options": name},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
        for name, color in colors.items()
    ]


def target(query: str, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DATASOURCE, "query": query}


def panel(kind, title, description, grid, targets, unit="short", overrides=(), options=None, custom=None, **defaults):
    return {
        "type": kind,
        "title": title,
        "description": description,
        "gridPos": dict(zip(("x", "y", "w", "h"), grid)),
        "datasource": DATASOURCE,
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "color": {"mode": "palette-classic"}, "custom": custom or {}, **defaults},
            "overrides": list(overrides),
        },
        "options": options or {},
    }


def timeseries(title, description, grid, targets, unit, bars=False, overrides=()):
    custom = {
        "drawStyle": "bars" if bars else "line",
        "stacking": {"mode": "normal" if bars else "none"},
        "fillOpacity": 80 if bars else 10,
        "lineWidth": 1 if bars else 2,
        "showPoints": "never",
        "spanNulls": True,
    }
    options = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}}
    p = panel("timeseries", title, description, grid, targets, unit, overrides, options, custom)
    p["interval"] = "1m"  # never aggregate finer than 1 minute
    return p


def row(title: str, y: int) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def custom_variable(name: str, label: str, values: list[str]) -> dict:
    return {
        "type": "custom",
        "name": name,
        "label": label,
        "query": ",".join(values),
        "multi": True,
        "includeAll": True,
        "allValue": ".*",  # "All" matches every value, including ones not listed (e.g. "unknown")
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
    }


# --- the dashboard ---

def build() -> dict:
    tenants = yaml.safe_load(TENANTS_FILE.read_text())["tenants"]
    tenant_colors = color_overrides(TENANT_COLORS)
    quotas = ", ".join(f'"{name}": {float(t["tpm_quota"])}' for name, t in tenants.items())

    panels = [
        row("FinOps: what are we spending, and on what?", 0),
        panel(
            "stat", "Total spend", "Sum of LiteLLM's response_cost for the selected time range, tenants and gateways.",
            (0, 1, 6, 8),
            [target(flux(*source("cost_usd"), "  |> group()", "  |> sum()"))],
            unit="currencyUSD", decimals=4,
            options={"reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "colorMode": "value",
                     "graphMode": "none", "textMode": "value"},
        ),
        timeseries(
            "Spend over time by tenant", "Spend per time bucket, stacked: the top of each bar is the total.",
            (6, 1, 18, 8),
            [target(flux(*source("cost_usd"), *per_tenant_over_time("sum", fill="0.0")))],
            unit="currencyUSD", bars=True, overrides=tenant_colors,
        ),
        panel(
            "barchart", "Spend by model", "Which model the money went to (the model that answered, after any fallback).",
            (0, 9, 12, 8),
            [target(flux(
                *source("cost_usd", 'r.model_used != "none"'),
                '  |> group(columns: ["model_used"])',
                "  |> sum()",
                '  |> keep(columns: ["model_used", "_value"])',
                "  |> group()",
                '  |> rename(columns: {_value: "spend"})',
            ))],
            unit="currencyUSD", decimals=4,
            options={"xField": "model_used", "orientation": "horizontal", "showValue": "always",
                     "legend": {"showLegend": False}},
        ),
        panel(
            "gauge", "Peak tokens per minute vs quota",
            "The busiest 1-minute bucket of each tenant as a % of its tpm_quota (from tenants.yaml). "
            "Near 100% means the tenant is about to be rate-limited.",
            (12, 9, 12, 8),
            [target(flux(
                f"quotas = [{quotas}]  // tpm_quota per tenant, from tenants.yaml",
                *source("total_tokens"),
                "  |> filter(fn: (r) => dict.get(dict: quotas, key: r.tenant, default: 0.0) > 0.0)",
                '  |> group(columns: ["tenant"])',
                "  |> aggregateWindow(every: 1m, fn: sum, createEmpty: false)",
                "  |> max()",
                "  |> map(fn: (r) => ({r with _value: float(v: r._value) / dict.get(dict: quotas, key: r.tenant, default: 1.0) * 100.0}))",
                *total_per_tenant(),
                imports=("dict",),
            ))],
            unit="percent", min=0, max=100,
            thresholds={"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "#EAB839", "value": 70}, {"color": "red", "value": 90}]},
            color={"mode": "thresholds"},  # the gauge color says how close to the limit, not which tenant
            options={"reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                     "showThresholdMarkers": True, "showThresholdLabels": False},
        ),

        row("Reliability: is it working?", 17),
        timeseries(
            "Requests per minute by tenant", "Every request, successful or not.",
            (0, 18, 12, 8),
            [target(flux(
                *source("prompt_tokens"),
                '  |> group(columns: ["tenant"])',
                "  |> aggregateWindow(every: v.windowPeriod, fn: count, createEmpty: true)",
                "  // count per bucket → per minute, whatever bucket size Grafana picked",
                "  |> map(fn: (r) => ({r with _value: float(v: r._value) * 60000000000.0 / float(v: int(v: v.windowPeriod))}))",
                *one_column_per("tenant"),
            ))],
            unit="reqpm", overrides=tenant_colors,
        ),
        timeseries(
            "Error rate by status (%)",
            "Share of requests per bucket that failed: 429 (our budget or a cooled-down model), other 4xx, 5xx.",
            (12, 18, 12, 8),
            [target(flux(
                *source("prompt_tokens"),
                "  |> map(fn: (r) => ({r with class:",
                '       if r.status_code == "429" then "e429"',
                '       else if strings.hasPrefix(v: r.status_code, prefix: "4") then "e4xx"',
                '       else if strings.hasPrefix(v: r.status_code, prefix: "5") then "e5xx"',
                '       else "ok"}))',
                '  |> group(columns: ["class"])',
                "  |> aggregateWindow(every: v.windowPeriod, fn: count, createEmpty: false)",
                *one_column_per("class"),
                "  |> map(fn: (r) => {",
                "       ok = if exists r.ok then r.ok else 0",
                "       e429 = if exists r.e429 then r.e429 else 0",
                "       e4xx = if exists r.e4xx then r.e4xx else 0",
                "       e5xx = if exists r.e5xx then r.e5xx else 0",
                "       total = float(v: ok + e429 + e4xx + e5xx)",
                '       return {_time: r._time, "429": float(v: e429) / total * 100.0,',
                '               "4xx": float(v: e4xx) / total * 100.0, "5xx": float(v: e5xx) / total * 100.0}',
                "     })",
                imports=("strings",),
            ))],
            unit="percent", overrides=color_overrides(ERROR_COLORS),
        ),
        timeseries(
            "Failovers over time", "Requests answered by a fallback model (fallback = true).",
            (0, 26, 12, 8),
            [target(flux(*source("prompt_tokens", 'r.fallback == "true"'), *per_tenant_over_time("count")))],
            unit="short", bars=True, overrides=tenant_colors,
        ),
        timeseries(
            "Latency p50 / p95 by model", "Successful requests only, by the model that answered. Dashed = p95.",
            (12, 26, 12, 8),
            [target(flux(
                *source("latency_ms", 'r.status_code == "200"'),
                '  |> group(columns: ["model_used"])',
                f"  |> aggregateWindow(every: v.windowPeriod, fn: (column, tables=<-) => tables |> quantile(q: {q}, column: column), createEmpty: false)",
                f'  |> map(fn: (r) => ({{r with series: r.model_used + " {name}"}}))',
                *one_column_per("series"),
            ), ref) for ref, q, name in (("A", 0.5, "p50"), ("B", 0.95, "p95"))],
            unit="ms",
            overrides=[{"matcher": {"id": "byRegexp", "options": ".* p95$"},
                        "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}],
        ),

        row("Security: what is the gateway protecting against?", 34),
        timeseries(
            "PII redactions over time by tenant", "Values the PII hook replaced before they could reach a model.",
            (0, 35, 12, 8),
            [target(flux(*source("pii_redactions"), *per_tenant_over_time("sum", fill="0")))],
            unit="short", bars=True, overrides=tenant_colors,
        ),
        panel(
            "bargauge", "Rate-limited requests (429) by tenant",
            "Requests our token budget rejected (rate_limited = true). Router cooldown 429s are not counted here.",
            (12, 35, 12, 8),
            [target(flux(
                *source("prompt_tokens", 'r.rate_limited == "true"'),
                '  |> group(columns: ["tenant"])',
                "  |> count()",
                *total_per_tenant(),
            ))],
            unit="short", overrides=tenant_colors, min=0,  # bars start at 0, even with only one tenant
            displayName="${__field.name}",  # the column name is the tenant; Grafana hides it for a lone bar otherwise
            options={"orientation": "horizontal", "displayMode": "basic", "valueMode": "color", "showUnfilled": True,
                     "namePlacement": "top",  # above the bar: a left-hand name gets cut off ("marketi...")
                     # fixed bar height and text size, so one tenant looks like three, not one giant bar
                     "sizing": "manual", "minVizHeight": 36, "maxVizHeight": 36,
                     "text": {"titleSize": 16, "valueSize": 20},
                     "reduceOptions": {"calcs": ["lastNotNull"], "values": False}},
        ),
    ]
    for panel_id, p in enumerate(panels, start=1):
        p["id"] = panel_id

    return {
        "uid": "aegis-gateway",
        "title": "Aegis - AI Gateway",
        "description": "Cost, reliability and security of the Aegis AI gateway. Generated by grafana/build_dashboard.py.",
        "tags": ["aegis"],
        "timezone": "browser",
        "editable": False,  # change grafana/build_dashboard.py instead
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "30s",
        "templating": {"list": [
            custom_variable("tenant", "Tenant", list(tenants)),
            custom_variable("gateway", "Gateway", ["main", "sandbox"]),
        ]},
        "annotations": {"list": []},
        "panels": panels,
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT}")
