"""Prints Grafana alert notifications to the console (its container log).

Grafana sends alerts to a webhook; this is that webhook. One line per alert:
    ALERT firing tenant=support summary="Tenant support spent 0.0712 USD in the last hour, ..."
Swap the contact point in grafana/provisioning/alerting/ for Slack/email/PagerDuty later.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        for alert in body.get("alerts", []):
            labels = alert.get("labels", {})
            summary = alert.get("annotations", {}).get("summary", "")
            print(f'ALERT {alert.get("status", "?")} tenant={labels.get("tenant", "-")} '
                  f'rule="{labels.get("alertname", "-")}" summary="{summary}"', flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # keep the output to the alert lines
        pass


if __name__ == "__main__":
    print("alert logger listening on :8080", flush=True)
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
