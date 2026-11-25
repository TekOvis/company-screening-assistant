# Deploying the demo

**Status:** the Dockerfile has not been built yet, because Docker is not available on the
development machine. The first build and smoke test will happen on the server, as a
separate step.

## What runs where

| | Local machine | Demo server |
|---|---|---|
| Backend | `mock`, `claude_code` (your subscription) or `anthropic` | `anthropic` (or `mock`) |
| Access | open | access token required (`PUBLIC_DEMO=true`) |
| Limits | defaults | strict per-IP and daily limits (below) |

**Never use `claude_code` on the server.** Anthropic does not allow offering claude.ai
login or subscription rate limits in products for other people. The image does not even
contain the Agent SDK, and `PUBLIC_DEMO=true` refuses that backend at startup.

## Spending protection, from the outside in

1. **Provider limit.** In the Anthropic Console:
   - create a separate workspace and API key for the demo;
   - give the workspace a monthly spend limit and email notifications.

   According to the documentation, requests are rejected once a limit is reached (HTTP
   400 for a custom limit, 429 for the tier cap). It does not say how quickly the limit
   takes effect, so the limits below remain the main protection. A notification is not a
   limit.
2. **Access token.** `DEMO_ACCESS_TOKEN` is required for every page and the API. Login
   attempts are limited per IP.
3. **Per-IP limit.** `RUNS_PER_IP_PER_HOUR` limits screenings from one address.
4. **Daily budget.** `DAILY_MAX_COMPANIES` and `DAILY_MAX_TOKENS` cover the whole server.
   Each run reserves its token cap from what is left for the day, so concurrent runs
   cannot overspend.
5. **Per-run and per-company budgets.** Turns, tool calls, requests and tokens are capped
   as described in `docs/architecture.md`.
6. **Kill switch.** Set `LLM_BACKEND=mock` and restart, or stop the container.

All app-side counters live in memory and reset when the container restarts.

## Server prerequisites

- Linux with Docker.
- A reverse proxy with HTTPS. Caddy is shown below; it gets certificates automatically
  for a domain name.
- A domain name pointing to the server. Without one, browsers cannot get a trusted
  certificate. Then set `COOKIE_SECURE=false` and keep the demo private: the token would
  travel unencrypted.

## Steps

1. **Get the code** onto the server, from the repository once it is published:

   ```bash
   git clone <repository-url> company-screening-assistant
   cd company-screening-assistant
   docker build -t screener:0.1.0 .
   ```

2. **Create the env file** outside the repository, readable by root only:

   ```bash
   sudo install -d -m 700 /etc/screener
   sudo install -m 600 /dev/null /etc/screener/screener.env
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # access token
   ```

   ```ini
   # /etc/screener/screener.env
   LLM_BACKEND=anthropic
   ANTHROPIC_API_KEY=...             # the demo workspace's key
   LLM_MODEL=claude-sonnet-5-5
   PUBLIC_DEMO=true
   DEMO_ACCESS_TOKEN=...             # 16+ characters, share it only with reviewers
   COOKIE_SECURE=true
   MAX_ACTIVE_RUNS=1
   RUNS_PER_IP_PER_HOUR=5
   DAILY_MAX_COMPANIES=50
   DAILY_MAX_TOKENS=500000           # ceiling about $5/day at Sonnet 5.5 output prices
   ```

3. **Run the container**, reachable only from the server itself:

   ```bash
   docker run -d --name screener --restart unless-stopped \
     --env-file /etc/screener/screener.env \
     -p 127.0.0.1:8000:8000 \
     --read-only --tmpfs /tmp \
     --memory 512m --cpus 1 \
     --security-opt no-new-privileges \
     screener:0.1.0
   ```

4. **Reverse proxy** (`/etc/caddy/Caddyfile`):

   ```caddyfile
   demo.your-domain.tld {
       encode gzip
       reverse_proxy 127.0.0.1:8000
       header {
           Strict-Transport-Security "max-age=31536000"
           X-Content-Type-Options "nosniff"
           X-Frame-Options "DENY"
           Referrer-Policy "no-referrer"
       }
   }
   ```

   Caddy sets `X-Forwarded-For`, and uvicorn (`--proxy-headers`) uses it as the client
   IP for the per-IP limits. That is safe only because the container port is published on
   `127.0.0.1`.

## Checks after deploying

- `curl -fsS https://demo.your-domain.tld/healthz` returns `"status": "ok"` and the
  expected backend.
- Without the token, `/` redirects to `/login` and `POST /api/screen` returns 401.
- With the token, one screening of one company works, and its token usage appears on the
  results page.
- After `RUNS_PER_IP_PER_HOUR` screenings the next one gets 429.
- `docker logs screener` contains no keys or tokens.

## Updating and rolling back

1. Build a new tag, for example `screener:0.1.1`.
2. Stop and remove the running container.
3. Start the new tag with the same command.
4. To roll back, start the previous tag. No data migrations are involved: runs live only
   in memory.
