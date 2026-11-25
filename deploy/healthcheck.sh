#!/usr/bin/env bash
# External health check for the demo. company-screener-health.timer runs it every minute.
#
# - Only a running service is checked. A stopped, failed or (re)starting demo is left
#   alone, and the restart is `systemctl try-restart`, which never starts a stopped
#   service.
# - GET /healthz with a 5 s timeout. A restart only after 3 failed checks in a row of the
#   same service instance; a manual restart starts the count again.
# - At most 3 restarts by this check within an hour. Past that it stops restarting and
#   says so once in the journal, until the service answers again (after a manual fix).
#   So a broken release is not restarted in an endless loop.
# - Healthy checks are not logged. Failures, restarts, giving up and recovery are:
#   journalctl -u company-screener-health
#
# HEALTH_SERVICE and HEALTH_URL may be overridden to test the script on another unit.
set -uo pipefail

SERVICE=${HEALTH_SERVICE:-company-screener}
URL=${HEALTH_URL:-http://127.0.0.1:8001/healthz}
STATE=${STATE_DIRECTORY:?run it from its systemd unit, which sets StateDirectory=}
TIMEOUT_S=5
FAILURES_BEFORE_RESTART=3
MAX_RESTARTS=3
RESTART_WINDOW_S=3600

failures_file=$STATE/failures # "<service invocation id> <failed checks in a row>"
restarts_file=$STATE/restarts # one line per restart by this check: epoch seconds
gave_up_file=$STATE/gave-up

if [[ $(systemctl is-active "$SERVICE") != active ]]; then
    rm -f "$failures_file" # stopped on purpose, failed, or (re)starting: not ours to touch
    exit 0
fi
invocation=$(systemctl show --property=InvocationID --value "$SERVICE")

if error=$(curl --silent --show-error --fail --max-time "$TIMEOUT_S" --output /dev/null \
    "$URL" 2>&1); then
    if [[ -e $gave_up_file ]]; then
        echo "<5>$SERVICE answers again; automatic restarts are allowed again"
        rm -f "$gave_up_file"
    elif [[ -s $failures_file ]]; then
        echo "<5>$SERVICE answers again"
    fi
    rm -f "$failures_file"
    exit 0
fi

if [[ -e $gave_up_file ]]; then
    exit 0 # already reported once; waiting for a person
fi

last_invocation="" count=0
if [[ -s $failures_file ]]; then
    read -r last_invocation count <"$failures_file"
fi
if [[ $last_invocation != "$invocation" ]]; then
    count=0
fi
count=$((count + 1))
echo "$invocation $count" >"$failures_file"
echo "<4>health check failed ($count of $FAILURES_BEFORE_RESTART in a row): ${error:-no details}"
if ((count < FAILURES_BEFORE_RESTART)); then
    exit 0
fi

now=$(date +%s)
touch "$restarts_file"
awk -v since=$((now - RESTART_WINDOW_S)) '$1 >= since' "$restarts_file" >"$restarts_file.new"
mv "$restarts_file.new" "$restarts_file"
recent=$(wc -l <"$restarts_file")
if ((recent >= MAX_RESTARTS)); then
    touch "$gave_up_file"
    echo "<3>$SERVICE does not answer, but this check already restarted it $recent times" \
        "within an hour; not restarting again until it answers. Needs a person:" \
        "journalctl -u $SERVICE"
    exit 0
fi

echo "$now" >>"$restarts_file"
rm -f "$failures_file"
echo "<4>restarting $SERVICE after $count failed checks in a row" \
    "(restart $((recent + 1)) of at most $MAX_RESTARTS within an hour)"
systemctl try-restart --no-block "$SERVICE"
