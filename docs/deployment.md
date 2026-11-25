# Deploying the demo

**Status (2026-11-25, 21:14 UTC):** the mock demo is live at
<https://screener.rustwatchtower.net>, release `7804bc4373aa`, with an external health
check every minute. An access token is required. See
[Activation record](#activation-record-2026-11-25) and
[Update: health check](#update-health-check-2026-11-25-2112-utc).
- It runs as a **mock-mode** service on a small VPS shared with another service, without
  Docker. See [Shared VPS without Docker](#shared-vps-without-docker-planned-first-demo).
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
| `healthcheck.sh`, `company-screener-health.service`, `company-screener-health.timer` | external health check every minute (see [Health check](#health-check)) |
| `caddy-detach.sh` | removes only the screener block from the current Caddyfile, validates, reloads (see [Rollback](#rollback)) |

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
- **Memory** hardly depends on parallelism: pages are parsed one at a time.
- **CPU is the larger cost.** A maximum job keeps one core busy for close to a minute on
  the development machine. The CPU times vary between runs on that machine.
- **Responsiveness.** These runs parsed HTML in the event loop. While a large page was
  parsed, the screener answered nothing else. That was later measured and changed (next
  section).

These are measurements on another OS and machine. The server check below repeats them.

### `/healthz` during the largest job (local, 2026-11-25)

Before adding an external health check with a 5 s timeout, `/healthz` was timed during
the largest job with 1 MB pages.
- The app ran under uvicorn on loopback.
- A separate thread asked `/healthz` over HTTP about once a second, like an external
  check.
- The synthetic fetcher waits 50 ms per page, like a fast network. Without any wait, a
  first version of the script never let the event loop answer: one check took 34 s,
  which overstates the problem.

| Parsing | `/healthz` median | max | checks over 5 s | Peak memory | CPU time |
|---|---|---|---|---|---|
| in the event loop (before) | 4.73 s | 7.13 s | 5 of 12 | 139 MB | 49.9 s |
| two worker threads (tried) | 0.57 s | 0.99 s | 0 of 47 | 155 MB | 45.7 s |
| **one parser thread (now)** | **0.33 s** | **0.77 s** | **0 of 53** | **142 MB** | 50.1 s |

With parsing in the event loop, the health check would have restarted the service in
the middle of an ordinary large job. On the server (slower CPU, `CPUQuota=50%`) the delays
would be longer still. Parsing now runs in one dedicated thread:
- One thread, not more: parsing is CPU-bound Python, so more threads are not faster (the
  GIL) but hold more pages in memory and slow the event loop down more.
- If a time limit fires, a page waiting for the parser is dropped. A page being parsed is
  finished, and the result is discarded.

**Not measured on the server.** The server is slower and limited to 50% of a core. If its
delays were a few times the local maximum of 0.77 s, they would still stay below 5 s.
That is an estimate, not a measurement.

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
- **Full HTTPS requests were about 1.8 times slower** (median +77 ms) while the job ran.
  *Assumed cause, not confirmed by measurement:* TLS and proxy work in Caddy competing
  with the job for the single core.

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

**A memory limit does not prevent hangs.** Two mechanisms bound a stuck screening:
- **Per screening:** `RUN_TIMEOUT_S` (300 s) stops it. Unfinished companies are skipped,
  finished results are kept, and the run's slot is freed. The same holds for an internal
  error. Tests cover a site that never answers, the JSON API and an internal error.
- **Per page:** fetching has its own connect, read and total timeouts.

These bounds do not cover a blocked event loop. Then the time limit cannot fire,
`/healthz` stops answering, and neither `Restart=on-failure` nor `MemoryMax` notices it.
The [health check](#health-check) covers that case.

What the health check does **not** cover:
- **A slow page in the parser thread.** The event loop stays free, so `/healthz` keeps
  answering and nothing is restarted. Later pages wait for the parser, and screenings
  end through the fetch and run time limits with errors rather than hang. Pages are
  capped at 1 MB, which makes this less likely, but it is not excluded.
- **Wrong answers.** `/healthz` only shows that the app answers, not that screenings
  work.

**Decision (2026-11-25): keep `CPUQuota=50%` and `FETCH_MAX_BYTES=1000000` for now.**
About 77 ms more on the other site's HTTPS during a maximum job is acceptable for this
demo. A lower quota would stretch each job. Ordinary use is to be watched first.

If the slower HTTPS during a large job becomes a problem, there are three options:
- lower `CPUQuota`, so jobs take longer but leave more of the core free;
- lower `FETCH_MAX_BYTES`: at 0.5 MB, local CPU time per job roughly halved;
- allow fewer URLs per screening.

These limits reduce the effect on the other service, but **do not exclude it**:
- the disk, the network, the page cache, the kernel and the single CPU core stay shared;
- even capped at 50%, a long job competes for the core and its caches.

### Health check

`company-screener-health.timer` runs `/opt/company-screener/bin/healthcheck.sh` every
minute, as a short root service with a hardened sandbox. The rules:
- **Only a running service.** If `company-screener` is stopped, failed or (re)starting,
  the check does nothing and forgets earlier failures. The restart command is
  `systemctl try-restart`, which never starts a stopped service, so a demo stopped on
  purpose stays stopped. The check unit has no `Wants=`/`Requires=` on the demo.
- **The check.** `GET http://127.0.0.1:8001/healthz` with a 5 s timeout.
- **When to restart.** After 3 failed checks in a row of the same service instance; a
  manual restart starts the count again.
- **No endless loop.**
  - The check restarts at most 3 times within an hour. After that it logs once that it
    gives up and does not restart again until the service answers (after a manual fix).
  - The app unit itself has `StartLimitIntervalSec=600` and `StartLimitBurst=5`: after 5
    starts within 10 minutes (crashes, the check, manual restarts) systemd leaves it
    failed. To start it again: `systemctl reset-failed company-screener`, then
    `systemctl start company-screener`.
- **Journal.** Healthy checks are not logged, and the routine start and finish lines are
  filtered out (`LogLevelMax=notice`). Failures with curl's reason, restarts, giving up
  and recovery are logged: `journalctl -u company-screener-health`.

The script lives outside the releases (`/opt/company-screener/bin`), so switching or
removing a release never breaks it. `install.sh` updates it with each release.

### Install without activating

`install.sh` runs as root on the server. It:
- creates the `screener` user;
- unpacks one commit into `/opt/company-screener/releases/<commit>` with its own venv;
- writes `/etc/company-screener/screener.env` once (mode 600, random token, never
  printed);
- installs the app unit, the health check unit and timer, and the health check script,
  and checks the units with `systemd-analyze verify`;
- backs up the previous units, script and release pointer to
  `/var/backups/company-screener/<time>/`;
- validates the settings and imports the app as the service user.

It does **not** enable or start the service or the timer, touch DNS, or edit or reload
Caddy. A test checks that the script contains no such command. A running service keeps
the previous release in memory until `systemctl restart company-screener`.

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
6. **Health check.** One manual run (silent when healthy), then the timer:

   ```bash
   systemctl start company-screener-health.service
   systemctl enable --now company-screener-health.timer
   systemctl list-timers company-screener-health.timer
   ```

### Rollback

An ordinary rollback undoes one layer and leaves everything else in place.

| Undo | Commands |
|---|---|
| **Release**, back to the previous one | `ln -sfn /opt/company-screener/releases/<previous> /opt/company-screener/current.new && mv -T /opt/company-screener/current.new /opt/company-screener/current`, then `systemctl restart company-screener` and `curl -fsS http://127.0.0.1:8001/healthz`. The units and the health check script stay those of the newer install; check that its `ExecStart` fits the older release |
| **Stop the demo** | `systemctl disable --now company-screener`. The health check leaves a stopped service alone |
| **Health check only** | `systemctl disable --now company-screener-health.timer` |
| **Caddy** | `bash <release>/deploy/caddy-detach.sh --dry-run`, read the change, then run it without `--dry-run`. See below |

**Caddy: remove only the screener block.** `caddy-detach.sh` (in releases from
`7804bc4373aa` on):
- edits the current `/etc/caddy/Caddyfile` and removes only the
  `screener.rustwatchtower.net` block and its comment lines;
- refuses to change anything unless it finds exactly one such block;
- validates the result as the caddy user and keeps a copy of the previous file
  (`/etc/caddy/Caddyfile.bak-<time>`);
- reloads, and puts the previous file back if the reload fails.

A saved copy of the whole Caddyfile also holds the other sites as they were then.
Restoring it would undo any later change to them. Use such a copy only as a last resort,
after `diff` shows that nothing else has changed since.

#### Removing the demo completely

This is **not** a rollback. It deletes the demo, its settings (including the access
token) and, if wanted, its backups. Do it only on an explicit decision.

```bash
bash /opt/company-screener/releases/<release>/deploy/caddy-detach.sh   # first, while the script exists
systemctl disable --now company-screener-health.timer company-screener
rm /etc/systemd/system/company-screener.service \
   /etc/systemd/system/company-screener-health.service \
   /etc/systemd/system/company-screener-health.timer
systemctl daemon-reload
rm -rf /opt/company-screener /etc/company-screener /var/lib/company-screener-health
userdel screener
# only if the backups and install archives are no longer wanted:
rm -rf /var/backups/company-screener
rm /root/screener-*.tar.gz
# finally, delete the A record "screener" at the DNS provider
```

### Activation record (2026-11-25)

**Releases.**
- `d9285ddfbf92` was installed first, without activation.
- `9f78dd0d9af9` brought the unit without `MemoryHigh`. Its first start failed with
  `203/EXEC`: `install.sh` had built the venv in a temporary directory and moved it, so
  `venv/bin/uvicorn` pointed at a missing interpreter.
- `9c3079ed1570` fixed this. It builds the venv in place, starts the app with
  `python -m uvicorn`, and `install.sh` checks that command. The service ran this release
  until the [update](#update-health-check-2026-11-25-2112-utc).
- The two older releases had the same venv problem. They were removed in that update.

**Checks** (all passed):
- **Loopback.**
  - mock backend;
  - without the token, `/` redirects to `/login`, and the API and `/openapi.json` answer
    401; a wrong token is refused;
  - with the token, the banner and 5 examples are shown; example 01 gives `fit`, as
    expected;
  - JSON and CSV export work.
- **HTTPS.**
  - Let's Encrypt certificate;
  - HSTS, `nosniff`, `DENY` and `no-referrer` headers, no `Server` header;
  - the same refusals without the token, from the server and from outside;
  - login through the form, with an `HttpOnly; Secure; SameSite=lax` cookie;
  - example 04 gives `insufficient_information`, as expected; the CSV downloads as an
    attachment.
- **Slot after an error.** A screening of an unresolvable host ended with `fetch_error`.
  The next screening was accepted at once (303).
  - The time limit itself was tested locally (`tests/test_run_limits.py`), not on the
    server.
- **Token.** It is not in any URL: login posts it in the body and redirects to `/`. It
  was not found in:
  - the service or Caddy journals, `/var/log/caddy`, syslog, or the release directories;
  - the Git working tree or history, or the session's temporary files.
- **The other service.** Its site and API answered 200 with normal response times, and
  all three services were active.

**Memory of the running service**, after two small examples and one failed screening.
This was not a maximum job.

| Source | Current | Peak |
|---|---|---|
| systemd `MemoryCurrent` / `MemoryPeak` | 83 MB | 83 MB |
| cgroup `memory.current` / `memory.peak` | 83 MB | 83 MB |
| process `VmRSS` / `VmHWM` | 98.5 MB | 98.5 MB |

**Discrepancies, kept separate from the results.**
- **Process vs cgroup.** The process's RSS is about 15 MB higher than the cgroup's
  count. A plausible but unverified explanation: RSS includes shared file pages (the
  Python binary, libraries) that are charged to the cgroup that loaded them first.
- **The transient load-check unit.** systemd reported `Memory peak: 1.3M`, while the
  process measured 129 MB. This is unexplained.
- **So 129 MB is not a confirmed peak of the service.** It is a single process-level
  measurement from the transient test. The service's cgroup peak under a maximum job has
  not been measured.

**Backups and rollback.**

| What | Where / how |
|---|---|
| Caddyfile before the change | `/var/backups/company-screener/caddy-20261125T193136Z/Caddyfile`, also `/etc/caddy/Caddyfile.bak-20261125T193136Z`. Kept as history; to undo Caddy, use `caddy-detach.sh` ([Rollback](#rollback)), not these copies |
| Units and release pointers before each install | `/var/backups/company-screener/20261125T192759Z/` (unit with `MemoryHigh`, pointer `d9285ddfbf92`) and `…/20261125T193045Z/` (unit of `9f78dd0d9af9`, pointer `9f78dd0d9af9`). Both units use `venv/bin/uvicorn` and would fail with `203/EXEC`, and both releases they point to have since been removed: history only |
| Stop the demo | `systemctl disable --now company-screener` |
| Remove everything | a separate operation, not a rollback: [Removing the demo completely](#removing-the-demo-completely) |

### Update: health check (2026-11-25, 21:12 UTC)

**What changed:** release `7804bc4373aa` (commits `ff7a7c4` and `7804bc4`):
- HTML parsing in one parser thread (see
  [`/healthz` during the largest job](#healthz-during-the-largest-job-local-2026-11-25));
- the [health check](#health-check): script, unit and timer;
- `StartLimitIntervalSec=600` and `StartLimitBurst=5` on the app unit;
- `caddy-detach.sh` for rolling back Caddy.

**Steps and checks** (all passed):
- **Install.** `install.sh` backed up the unit and the release pointer of `9c3079ed1570`
  to `/var/backups/company-screener/20261125T211228Z/`; the health check files did not
  exist before. Settings: mock, no API key. The app imports as the service user.
- **Restart onto the new release** at 21:12:34 UTC.
  - `/healthz` answered with the mock backend.
  - The process uses files from `7804bc4373aa` only.
  - systemd shows the start limit of 5 per 10 minutes.
- **Smoke test.**
  - Without the token, `/` redirects to `/login`.
  - Example 03 gave `non_fit`, as expected.
  - `https://screener.rustwatchtower.net/healthz` answered 200.
- **Health check logic, tested on a dummy unit, not on the demo.** A copy of the check
  unit with its own state directory watched a transient `sleep` unit, against a closed
  port:
  - runs 3, 6 and 9 restarted the dummy (3 failed checks in a row each, counted per
    instance);
  - run 12 gave up with one error line, and run 13 did nothing;
  - pointed at the real `/healthz`, the next run logged the recovery and allowed restarts
    again;
  - with the dummy stopped, 4 failing checks did nothing: it stayed stopped, and no
    failure was counted;
  - only the script's own lines reached the journal, without routine start and finish
    lines, at the intended levels: 1 error (giving up), 16 warnings (12 failures,
    3 restarts, giving up), 1 notice (recovery);
  - the test unit, the dummy and the test state were removed afterwards.
- **The real check.** One manual run succeeded and logged nothing. After the timer was
  enabled, it ran every minute from 21:14 UTC, each run ended with status 0, the journal
  stayed empty, and no failure was recorded (checked at 21:16 UTC).
- **Caddy rollback, dry run on the real Caddyfile.** `caddy-detach.sh --dry-run` would
  remove exactly the screener block (lines 31–47). The rest validated as the caddy user,
  the Caddyfile stayed unchanged, and no root-owned file appeared in `/var/log/caddy`.
- **Neighbours.** Rust Watchtower's API and site, and the German trainer site, answered
  200. All services were active.
- **Memory** after the restart and one example: `MemoryCurrent` 86.7 MB, `MemoryPeak`
  86.9 MB. This was not a large job.

**Removed releases.** First it was checked that `d9285ddfbf92` and `9f78dd0d9af9`:
- are not `current`;
- are not named in any unit;
- have no symbolic link pointing at them;
- are used by no process.

Then the two release directories (81 MB each) and their install archives in `/root` were
removed. Kept: release `7804bc4373aa` (current), release `9c3079ed1570` (the rollback
target), their archives, all backups, the settings and Caddy. The unit backups in
`20261125T192759Z` and `20261125T193045Z` still name the removed releases in
`previous-current.txt`. They are history only.

**Rolling back this update.**
- **Release:** back to `9c3079ed1570` with the symbolic link and a restart. Its
  `ExecStart` matches the current unit. That release parses HTML in the event loop, so
  the health check could restart the service during a large job. Disable the timer as
  well in that case.
- **Health check only:** `systemctl disable --now company-screener-health.timer`.
- **Caddy:** `bash /opt/company-screener/releases/7804bc4373aa/deploy/caddy-detach.sh`,
  with `--dry-run` first.

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
