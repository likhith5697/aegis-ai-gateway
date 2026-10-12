# Aegis AI Gateway

A self-hosted AI gateway built on the [LiteLLM](https://github.com/BerriAI/litellm) proxy. Your apps send OpenAI-format requests to Aegis. Aegis checks which tenant is calling, removes sensitive data from the prompt, and only then forwards the request to the model provider (Anthropic Claude).

This is a learning project, built one small step at a time.

## How a request flows

```
Client (OpenAI format)
   │  Authorization: Bearer <master key>
   │  Tenant-ID: support
   ▼
LiteLLM proxy (Docker, port 4000)
   1. Master key check          → 401/400 if wrong
   2. tenant_hook.py            → 400 missing Tenant-ID, 403 unknown tenant / model not allowed
   3. pii_hook.py               → replaces PII with [REDACTED_<TYPE>]
   ▼
Anthropic API (Claude) — only ever sees the redacted prompt
   ▼
Response + x-aegis-pii-redactions / x-aegis-pii-types headers
```

## Features

- **Multi-tenancy.** Every request must send a `Tenant-ID` header. Each tenant in `gateway/tenants.yaml` has a list of allowed models and a tokens-per-minute quota (quotas are not enforced yet).
- **PII scrubbing.** Before a request leaves the gateway, every message is scanned (all roles, including system and multi-part content), and these are replaced:

  | Data | Placeholder |
  |---|---|
  | US SSN (`123-45-6789`) | `[REDACTED_SSN]` |
  | Credit card numbers (Luhn-checked) | `[REDACTED_CREDIT_CARD]` |
  | Email addresses | `[REDACTED_EMAIL]` |
  | US phone numbers | `[REDACTED_PHONE]` |
  | API keys (`sk-...`, `AKIA...`, `ghp_...`, Slack, Google) | `[REDACTED_API_KEY]` |

  Logs record only the tenant and the count per type, never the original text. If the detector fails, the request is rejected (`503`) instead of being sent unscrubbed. Scrubbing is on by default and can be turned off per tenant.
- **Pluggable detection.** Detectors implement the `PIIDetector` interface in `gateway/pii.py`. A regex detector is used today; another detector, such as Amazon Comprehend, can be swapped in without changing the hook.

## Project layout

```
gateway/
  config.yaml      LiteLLM config: models, hooks (in run order), master key
  tenants.yaml     tenants, allowed models, quotas, pii_scrubbing flag
  tenants.py       loads tenants.yaml, reads the Tenant-ID header
  tenant_hook.py   hook 1: tenant and model checks
  pii.py           PIIDetector interface, RegexDetector, redact()
  pii_hook.py      hook 2: scrubs messages, logs counts, adds response headers
tests/
  Dockerfile       test image: the same LiteLLM image + pytest
  test_*.py        unit tests (no server, no API calls)
docker-compose.yml gateway service + tests service
.env.example       the environment variables you need
```

## Getting started

You only need [Docker Desktop](https://www.docker.com/products/docker-desktop/). Everything runs in containers, and all image and package versions are pinned.

1. Create your `.env` from the template:
   ```bash
   cp .env.example .env
   ```
   Then fill in:
   - `ANTHROPIC_API_KEY`: your Anthropic API key
   - `LITELLM_MASTER_KEY`: a password you choose for the gateway (must start with `sk-`)

   `.env` is git-ignored. Never commit it.

2. Start the gateway:
   ```bash
   docker compose up
   ```
   It's ready when the logs show `Uvicorn running on http://0.0.0.0:4000`.

## Usage

```bash
curl -i http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Tenant-ID: support" \
  -d '{
    "model": "claude-sonnet-5-5",
    "max_tokens": 300,
    "messages": [{"role": "user", "content": "Repeat exactly: my SSN is 123-45-6789"}]
  }'
```

The response includes `x-aegis-pii-redactions: 1` and `x-aegis-pii-types: SSN=1`, and the model replies with `[REDACTED_SSN]` because it never received the real number.

On Windows, run curl from Git Bash. In Windows PowerShell 5.1, `curl` is a different command and breaks the JSON quoting.

### Errors

| Situation | Status |
|---|---|
| Missing or blank `Tenant-ID` | `400` |
| Unknown tenant | `403` |
| Tenant asks for a model it's not allowed to use | `403` |
| PII detector failed | `503` |

## Configuring tenants

Edit `gateway/tenants.yaml` and restart the gateway:

```yaml
tenants:
  support:
    allowed_models: [claude-sonnet-5-5]
    tpm_quota: 50000
    pii_scrubbing: true   # optional, defaults to true
```

Model names must match a `model_name` in `gateway/config.yaml`. A malformed file stops the gateway at startup instead of failing on live requests.

## Running the tests

```bash
docker compose run --rm --build tests
```

The tests run inside the same LiteLLM image as the gateway. They call the hooks directly, so they need no running server, no API key and no credits.

## Known limitations

- `Tenant-ID` is a claim, not proof: anyone with the master key can send any tenant name. Per-tenant API keys would fix this.
- Tokens-per-minute quotas are stored but not enforced yet.
- PII scrubbing covers message content only. It doesn't cover tool-call arguments, the legacy `/completions` `prompt` field or embeddings input.
- The regex detector doesn't find names or street addresses, and it only handles US phone numbers and dashed SSNs.
- Don't run LiteLLM with `--detailed_debug`: it logs raw request bodies before the PII hook runs.
