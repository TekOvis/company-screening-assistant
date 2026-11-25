# Deploying the demo

**Status (2026-11-25):** installed on the server but **not activated**.
- Release `d9285ddfbf92`, installed with `install.sh`. The service is disabled and not
  running; there is no DNS record yet, and Caddy is unchanged.
- The planned first demo is a **mock-mode** service at `screener.rustwatchtower.net`, on a
  small VPS shared with another service, without Docker. See
  [Shared VPS without Docker](#shared-vps-without-docker-planned-first-demo).
- The Dockerfile route below is kept for other hosts. The image has not been built yet,
  because Docker is not available on the development machine.

## Shared VPS without Docker (planned first demo)

The demo will run on a VPS that already serves another application (Rust Watchtower),
managed by systemd behind Caddy.
- **Machine:** 1 vCPU, 2 GB RAM, Ubuntu 24.04, Python 3.12.
- **Why without Docker:** the existing service is set up the same way, and one consistent
  setup is simpler to operate and roll back. Docker itself would not necessarily add much
  load.

**What runs.**
- **Backend:** mock mode only, with no Claude Code and no API key on the server. Every
  page shows a "Mock demo" banner: the demo shows the interface and the pipeline, not the
  quality of an LLM.
- **Examples:** five ready-made synthetic examples (`DEMO_EXAMPLES=true`) run on saved
  pages and show their expected label next to the result. One of them is a case the mock
  gets wrong.
- **Access:** an access token is required (`PUBLIC_DEMO=true`).

**Files** in [`deploy/`](../deploy):

| File | Purpose |
|---|---|
| `company-screener.service` | systemd unit: user `screener`, `127.0.0.1:8001` only, resource limits, hardening |
| `screener.env.example` | server settings: mock, token required, job and storage limits |
| `screener.caddy` | the Caddy site block for `screener.rustwatchtower.net`, with a 64 KB request body limit |
| `requirements.txt` | runtime packages pinned with hashes, exported from `uv.lock` (`uv export --frozen --no-dev --no-emit-project`) |
| `install.sh` | prepares everything **without activating** it (below) |

**Limits on the job.**
- **Size:** at most 10 URLs per screening; pages up to 1 MB (`FETCH_MAX_BYTES`); 15,000
  characters of text kept per page; up to 3 extra pages per company; criteria fields of
  limited length; request bodies up to 64 KB (Caddy).
- **Parallelism:** one screening at a time (`MAX_ACTIVE_RUNS=1`), two companies of it at
  once (`CONCURRENCY=2`); uvicorn accepts at most 32 connections at once.
- **Storage:** results in memory only, at most 20 runs, each kept for at most 1 hour.
- **Abuse:** 6 screenings per IP per hour; 10 login attempts per IP per 15 minutes; 200
  companies per day.

### Measurements (local, Windows, 2026-11-25)

`scripts/measure_memory.py` runs the largest allowed job in-process: 10 sites, each with a
homepage and 3 subpages of the maximum page size, in mock mode. It reports the process's
peak memory and CPU time.

| Page limit | Screenings at once | Companies in parallel | Peak memory | CPU time |
|---|---|---|---|---|
| 2 MB | 1 | 2 | 152 MB | 54 s |
| 2 MB | 2 | 2 | 153 MB | 106 s |
| 2 MB | 1 | 3 | 152 MB | 61 s |
| 2 MB | 1 | 1 | 152 MB | 88 s |
| 1 MB (server setting) | 1 | 2 | 132 MB | 45 s |
| 0.5 MB | 1 | 2 | 122 MB | 23 s |

The process starts at about 95 MB (interpreter, imports, app). What the numbers show:
- **Memory** hardly depends on parallelism. HTML parsing runs synchronously in the event
  loop, so pages are parsed one at a time.
- **CPU is the larger cost.** A maximum job keeps one core busy for close to a minute on
  the development machine. The CPU times vary between runs on that machine.
- **Responsiveness.** While a large page is parsed (about 1 s), the screener itself does
  not answer other requests. This does not block other services, but it does make the
  demo less responsive. Moving parsing to a worker thread is a possible later
  improvement.

These are measurements on another OS and machine. The server check below repeats them.

### Load check on the server (2026-11-25, 19:12–19:21 UTC)

**Setup.**
- The same script ran as user `screener` in a transient systemd unit, under the
  service's limits, with the installed release `d9285ddfbf92`.
- The job was the largest the server settings allow: 10 sites × 4 pages of 1 MB, two
  companies in parallel.
- The service itself was not started, and Caddy was not changed.
- Every 2 seconds the other service was probed: its local API on loopback and its public
  HTTPS page (from the server itself).

**The job.**
- All 40 pages processed, status `ok`.
- Peak memory 129 MB (the process's own maximum RSS); 92 MB before the job.
- CPU time 65 s; wall time 130 s. That ratio matches `CPUQuota=50%`.

**The other service's response times** (seconds; 14, 63 and 10 samples):

| Phase | Local API: median / p95 / max | Public HTTPS: median / p95 / max | Load average (1 min), max |
|---|---|---|---|
| Before (30 s) | 0.006 / 0.078 / 0.078 | 0.091 / 0.119 / 0.119 | 0.17 |
| During the job (~2 min) | 0.007 / 0.023 / 0.041 | **0.168 / 0.212 / 0.248** | 1.14 |
| After (20 s) | 0.006 / 0.011 / 0.011 | 0.096 / 0.150 / 0.150 | 1.11 |

All responses were 200. The other service logged no warnings or errors in that window.
- Its application API was not slowed down.
- **Full HTTPS requests were about 1.8 times slower** (median +77 ms) while the job ran:
  TLS work in Caddy competes for the single core.

The limits reduced the effect, but did not remove it.

**Memory limit checks** (same user and limits, each test process stopped by a time
limit):
- **Accounting.** A process holding 200 MB showed `memory.current` and `memory.peak` of
  203 MB in its cgroup.
- **`MemoryMax=300M` alone.** A process that tried to take 400 MB was killed by the
  kernel (`oom-kill`, constrained to its own cgroup). Nothing else was affected.
- **With `MemoryHigh=250M` as well, and no swap,** the same process was not killed. It was
  throttled at 263 MB (43,432 `high` events) and hung until stopped by hand. For the
  service, that would mean a hang that `Restart=on-failure` does not catch, so
  **`MemoryHigh` was removed from the unit**.
- **Unexplained:** at the end of the load check, systemd reported `Memory peak: 1.3M` for
  the transient unit, while the process measured 129 MB. The direct cgroup reading above
  works, so check `MemoryPeak` again while the service is running.

### Resource limits and what they do not guarantee

The unit sets:
- **Memory:** `MemoryMax=300M`, `MemorySwapMax=0`, and no `MemoryHigh` (see above);
- **CPU:** `CPUQuota=50%`, `CPUWeight=20`, `Nice=10`;
- **Other:** `IOWeight=20`, `TasksMax=64`.

300 MB is about 2.3 times the peak measured on the server for the largest job. If the
service exceeds it, the kernel kills this service only, and systemd restarts it.

If the slower HTTPS during a large job is not acceptable, there are three options:
- lower `CPUQuota`, so jobs take longer but leave more of the core free;
- lower `FETCH_MAX_BYTES`: at 0.5 MB, local CPU time per job roughly halved;
- allow fewer URLs per screening.

These limits reduce the effect on the other service, but **do not exclude it**:
- the disk, the network, the page cache, the kernel and the single CPU core stay shared;
- even capped at 50%, a long job competes for the core and its caches.

The next step is a check under load on the server, while watching the other service's
response times.

### Install without activating

`install.sh` runs as root on the server. It:
- creates the `screener` user;
- unpacks one commit into `/opt/company-screener/releases/<commit>` with its own venv;
- writes `/etc/company-screener/screener.env` once (mode 600, random token, never
  printed);
- installs the unit and checks it with `systemd-analyze verify`;
- validates the settings and imports the app as the service user.

It does **not** enable or start the service, touch DNS, or edit or reload Caddy. A test
checks that the script contains no such command.

```bash
# on the development machine
git archive --format=tar.gz -o screener-<commit>.tar.gz <commit>
# copy it to the server, then on the server:
tar -xzf screener-<commit>.tar.gz -C /tmp deploy/install.sh
sudo bash /tmp/deploy/install.sh /root/screener-<commit>.tar.gz <commit>
```

### Activation, each step only after explicit approval

1. **DNS.** Add an A record `screener.rustwatchtower.net → <server IP>` at the DNS
   provider. Check it on the authoritative name servers, not only on a public resolver.
2. **Load check before the service is public.** Run the same measurement under the
   service's limits, and watch the other service's response time meanwhile:

   ```bash
   systemd-run --wait --pipe --uid=screener -p MemoryMax=300M -p CPUQuota=50% \
     -p CPUWeight=20 -E PYTHONPATH=/opt/company-screener/current/src \
     /opt/company-screener/current/venv/bin/python \
     /opt/company-screener/current/scripts/measure_memory.py --max-bytes 1000000
   ```

   Adjust `MemoryMax` from the measured peak if needed.
3. **Start.**

   ```bash
   systemctl enable --now company-screener
   systemctl is-active company-screener
   curl -fsS http://127.0.0.1:8001/healthz
   ```

4. **Caddy.** Back up the config, append the block, validate it **as the caddy user** and
   reload. Validating as root can leave a root-owned log file that stops Caddy.

   ```bash
   cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date -u +%Y%m%dT%H%M%SZ)
   cat /opt/company-screener/current/deploy/screener.caddy >> /etc/caddy/Caddyfile
   runuser -u caddy -- caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   systemctl reload caddy
   ```

5. **Checks.**
   - `https://screener.rustwatchtower.net/healthz` answers.
   - Without the token, `/` redirects to `/login`.
   - An example runs.
   - The other site still answers as before.
   - `systemctl show company-screener -p MemoryPeak` stays well below the limit.

### Rollback

| Undo | Commands |
|---|---|
| Caddy | `cp -a /etc/caddy/Caddyfile.bak-<time> /etc/caddy/Caddyfile`, validate as the caddy user, `systemctl reload caddy` |
| Service | `systemctl disable --now company-screener` |
| Release | point `/opt/company-screener/current` at the previous release (`ln -sfn … current.new && mv -T current.new current`), then `systemctl restart company-screener` |
| Everything | stop and disable the service; remove the unit and `systemctl daemon-reload`; `rm -rf /opt/company-screener /etc/company-screener`; `userdel screener`; remove the DNS record |

## Docker route (other hosts)

### What runs where

| | Local machine | Demo server |
|---|---|---|
| Backend | `mock`, `claude_code` (your subscription) or `anthropic` | `anthropic` (or `mock`) |
| Access | open | access token required (`PUBLIC_DEMO=true`) |
| Limits | defaults | strict per-IP and daily limits (below) |

**Never use `claude_code` on the server.** Anthropic does not allow offering claude.ai
login or subscription rate limits in products for other people. The image does not even
contain the Agent SDK, and `PUBLIC_DEMO=true` refuses that backend at startup.

### Spending protection, from the outside in

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

### Server prerequisites

- Linux with Docker.
- A reverse proxy with HTTPS. Caddy is shown below; it gets certificates automatically
  for a domain name.
- A domain name pointing to the server. Without one, browsers cannot get a trusted
  certificate. Then set `COOKIE_SECURE=false` and keep the demo private: the token would
  travel unencrypted.

### Steps

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

### Checks after deploying

- `curl -fsS https://demo.your-domain.tld/healthz` returns `"status": "ok"` and the
  expected backend.
- Without the token, `/` redirects to `/login` and `POST /api/screen` returns 401.
- With the token, one screening of one company works, and its token usage appears on the
  results page.
- After `RUNS_PER_IP_PER_HOUR` screenings the next one gets 429.
- `docker logs screener` contains no keys or tokens.

### Updating and rolling back

1. Build a new tag, for example `screener:0.1.1`.
2. Stop and remove the running container.
3. Start the new tag with the same command.
4. To roll back, start the previous tag. No data migrations are involved: runs live only
   in memory.
