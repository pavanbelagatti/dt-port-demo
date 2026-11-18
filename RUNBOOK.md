# dt-port-demo runbook

What to do when dt-port-demo misbehaves. Start with "First 5 minutes", then jump to the playbook that matches what you see.

> **This is a demo service.** Some failures are deliberate (see "Normal behaviour"). Check that before you page anyone.

## Service at a glance

| | |
| --- | --- |
| What it is | `shopdemo`, a Flask e-commerce app used for Datadog + Port demos |
| Owner | platform |
| Tier | tier-1 (as set in Port) |
| Slack | `#dt-port-demo-alerts` |
| Port entity | `dt-port-demo` (blueprint: `service`) |
| Datadog | service `dt-port-demo`, env `demo`, site `us5.datadoghq.com` |
| Dynatrace | `SERVICE-D51C8B6F5B66DD1E` (demo environment) |
| Outbound dependency | GitHub API (`UPSTREAM_RATES_URL`, default `https://api.github.com/zen`) |
| Health check | `GET /healthz` |

The app simulates four internal services in the same process. Each emits APM traces under its own name, so they show up as separate services in Datadog: `payments-service`, `auth-service`, `shipping-service` and `inventory-service`.

## Normal behaviour: don't page for this

These error rates are built in and expected:

| Endpoint | Expected failure rate |
| --- | --- |
| `POST /checkout` | about 5% |
| `GET /payments/process` | about 8% |
| `GET /auth/verify` | about 3% |
| `GET /shipping/track/<id>` | about 5% |
| `GET /inventory/check` | rarely fails |
| `GET /error` | always 500 (legacy test endpoint) |
| `GET /slow` | always slow (legacy test endpoint) |

The self-heal workflow treats an overall error rate above **5%** as worth escalating.

## First 5 minutes

1. **Is it up?** `curl https://<host>/healthz`
2. **Is a failure scenario switched on?** `curl https://<host>/scenarios/status`. If any mode is active, go to the matching playbook below. Most incidents on this service are a scenario someone left running.
3. **How bad is it?** In Datadog, check the error rate and p99 latency for service `dt-port-demo`, env `demo`. Compare against the normal rates above.
4. **Did something just change?** Check the latest commits and deploys on `main`.
5. **Tell people.** Post in `#dt-port-demo-alerts` that you're looking, and update the incident in Port.

## Playbooks

Every scenario can be switched off with its own reset endpoint, or all at once with `GET /scenarios/reset-all`.

### Payments failing, checkout errors spiking (cascade failure)

- **Symptoms:** checkout failures jump to about 60%, payments to about 70%, shipping to about 55%, with added latency. Affects `dt-port-demo`, `payments-service` and `shipping-service`.
- **Triggered by:** `GET /scenarios/cascade-failure`
- **Steps:** check payments-service first, since it's where the cascade starts. Then enable a circuit breaker and route to a fallback queue.
- **Reset:** `GET /scenarios/cascade-failure/reset`

### Logins failing (auth degradation)

- **Symptoms:** `/auth/verify` failures rise to about 45% with 401 responses ("token validation failed"), plus 1 to 3 seconds of extra latency.
- **Triggered by:** `GET /scenarios/auth-failure`
- **Steps:** check the JWT signing service, verify the token cache hit rate, then restart the auth pods.
- **Reset:** `GET /scenarios/auth-failure/reset`

### Everything is slow (latency spike)

- **Symptoms:** 1.5 to 4 seconds of extra latency across endpoints.
- **Triggered by:** `GET /scenarios/latency-spike`
- **Steps:** check CPU and memory, look for a noisy neighbour, then check the database connection pool.
- **Reset:** `GET /scenarios/latency-spike/reset`

### Slow database queries

- **Symptoms:** queries taking 2 to 8 seconds.
- **Triggered by:** `GET /scenarios/slow-db`
- **Steps:** check the slow query log, run `EXPLAIN` on the orders queries, and add an index on `created_at`.
- **Reset:** `GET /scenarios/slow-db/reset`

### Memory keeps growing (memory leak)

- **Symptoms:** steady, unbounded memory growth.
- **Triggered by:** `GET /scenarios/memory-leak`
- **Steps:** look for unbounded caches, session objects, or event listeners that aren't being cleared.
- **Reset:** `GET /scenarios/memory-leak/reset`

### Burst of 404s

- **Symptoms:** a sudden spike in 404 responses.
- **Triggered by:** `GET /scenarios/404-spike`
- **Steps:** check the recent deploy diff, verify the product SKU migration ran, and check CDN routing rules.
- **Reset:** the burst is one-off; `GET /scenarios/reset-all` clears any remaining state.

### Shipping rates failing

- **Symptoms:** `/shipping/rates` errors or slow responses, with `shipping_rates: upstream failed` in the logs.
- **Cause:** the outbound call to `UPSTREAM_RATES_URL` (GitHub API by default) is failing or rate limited.
- **Steps:** check https://www.githubstatus.com, confirm `UPSTREAM_RATES_URL` is set correctly, and check whether the host is hitting GitHub's rate limit.

## Automated response: the self-heal workflow

`.github/workflows/self-heal-incident.yml` runs when a Port automation dispatches it for a Datadog incident. It:

1. Pulls the service's owner, tier, runbook and Slack channel from Port.
2. Checks Datadog's live error rate for `dt-port-demo` over the last 5 minutes.
3. Auto-resolves if the error rate is at or below 5% and recovering, or escalates with full context if not.
4. Writes the root cause and resolution summary back to the Port incident.

**Run it by hand:** GitHub → **Actions** → **Self-Heal Incident (Port + Datadog)** → **Run workflow**. Enter the Port incident identifier (for example `dd-12345`) and leave the service as `dt-port-demo`.

**It needs these repository secrets:** `PORT_CLIENT_ID`, `PORT_CLIENT_SECRET`, `DD_API_KEY`, `DD_APP_KEY`.

**Known limitation:** the Slack webhook (`SLACK_WEBHOOK_URL`) is commented out, so the workflow prints its Slack messages in the run log instead of posting them. If Port has no Slack channel for the service, it falls back to `#incidents`.

## Run or restart the app

```bash
pip install -r requirements.txt
python app.py
```

The app listens on `PORT` (default `8080`).

| Variable | Purpose | Default |
| --- | --- | --- |
| `FLASK_SECRET` | Flask session secret | `dev-secret-do-not-use-in-prod`. Set a real value anywhere public. |
| `PORT` | Port to listen on | `8080` |
| `UPSTREAM_RATES_URL` | Outbound URL for `/shipping/rates` | `https://api.github.com/zen` |

Tracing uses `ddtrace` and OpenTelemetry, so a Datadog agent needs to be reachable for traces to show up.

## Escalation

1. Post in `#dt-port-demo-alerts` with what you've checked so far.
2. Contact the owning team: **platform**.
3. There's no on-call rotation set for this service in Port yet. Add one before relying on this runbook for real incidents.

## Keep this runbook current

- Update this file whenever you add a scenario endpoint or change a failure rate in `app.py`.
- Keep the runbook link on the `dt-port-demo` service in Port pointing at this file.
