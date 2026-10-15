"""Renders Grafana's alerting file from its template, before Grafana starts.

Grafana doesn't fill in environment variables inside alert rules, so the spend
threshold from .env is put in here. Runs as the short-lived "grafana-alerting"
container on every `docker compose up`; Grafana waits for it to succeed.
"""

import os
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "alerting" / "spend-alert.template.yaml"
PLACEHOLDER = "__AEGIS_SPEND_ALERT_USD__"


def render(threshold: str) -> str:
    """The alerting YAML with the threshold filled in. Rejects anything but a positive number."""
    try:
        value = float(threshold)
    except ValueError:
        raise ValueError(f"AEGIS_SPEND_ALERT_USD must be a number, got {threshold!r}") from None
    if value <= 0:
        raise ValueError(f"AEGIS_SPEND_ALERT_USD must be above 0, got {threshold!r}")
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, repr(value))


if __name__ == "__main__":
    out_dir = Path(os.environ.get("OUT_DIR", "/out"))
    try:
        rendered = render(os.environ.get("AEGIS_SPEND_ALERT_USD", "0.05"))
    except ValueError as e:
        sys.exit(f"grafana-alerting: {e}")
    (out_dir / "spend-alert.yaml").write_text(rendered, encoding="utf-8")
    print(f"grafana-alerting: wrote {out_dir / 'spend-alert.yaml'}")
