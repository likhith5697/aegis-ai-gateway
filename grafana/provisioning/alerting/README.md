Intentionally empty. Docker mounts the `grafana-alerting` volume over this folder; that volume
holds the alert rules rendered from `grafana/alerting/spend-alert.template.yaml` by
`grafana/render_alerting.py`. The folder must exist because its parent is mounted read-only.
