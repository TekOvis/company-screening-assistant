#!/usr/bin/env bash
# Prepare the Company Screening Assistant demo on this server WITHOUT activating it.
#
#   sudo bash install.sh /root/screener-<commit>.tar.gz <commit>
#
# The bundle is `git archive` of one commit. This script:
#   - creates the system user `screener` and /opt/company-screener/releases/<commit>;
#   - builds a venv for that release from deploy/requirements.txt (pinned, hashed, wheels only);
#   - creates /etc/company-screener/screener.env once (mode 600, random access token);
#   - installs the systemd unit and checks it with systemd-analyze;
#   - points /opt/company-screener/current at the release;
#   - checks the configuration and that the app imports.
# It does NOT enable or start the service, touch DNS, or edit or reload Caddy.
set -euo pipefail

BUNDLE=${1:?usage: install.sh BUNDLE.tar.gz COMMIT}
COMMIT=${2:?usage: install.sh BUNDLE.tar.gz COMMIT}
BASE=/opt/company-screener
RELEASE="$BASE/releases/$COMMIT"
ETC=/etc/company-screener
UNIT=/etc/systemd/system/company-screener.service
PORT=8001

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ $COMMIT =~ ^[0-9a-f]{7,40}$ ]] || { echo "bad commit id: $COMMIT" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info[:2] < (3, 12))' \
    || { echo "python3 >= 3.12 is required" >&2; exit 1; }
if ss -ltnH "sport = :$PORT" | grep -q .; then
    echo "port $PORT is already in use" >&2; exit 1
fi
if [[ -e $RELEASE ]]; then
    echo "release $COMMIT already exists: $RELEASE" >&2; exit 1
fi

echo "== user and directories"
id screener >/dev/null 2>&1 \
    || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin screener
install -d -m 755 -o root -g root "$BASE" "$BASE/releases"

echo "== release $COMMIT"
TMP="$RELEASE.partial"
rm -rf "$TMP"
install -d -m 755 "$TMP"
tar -xzf "$BUNDLE" -C "$TMP"
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir \
    --require-hashes --only-binary :all: --no-deps -r "$TMP/deploy/requirements.txt"
"$TMP/venv/bin/python" -m compileall -q "$TMP/src"
chown -R root:root "$TMP"
chmod -R go-w "$TMP"
mv "$TMP" "$RELEASE"

echo "== settings file (created once, never overwritten)"
install -d -m 700 -o root -g root "$ETC"
if [[ ! -f $ETC/screener.env ]]; then
    install -m 600 -o root -g root "$RELEASE/deploy/screener.env.example" "$ETC/screener.env"
    token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    sed -i "s|^DEMO_ACCESS_TOKEN=.*|DEMO_ACCESS_TOKEN=$token|" "$ETC/screener.env"
    echo "created $ETC/screener.env with a new access token (not printed)"
else
    echo "kept existing $ETC/screener.env"
fi

echo "== current release"
# Before the unit check: systemd-analyze verify needs the ExecStart binary to exist.
ln -sfn "$RELEASE" "$BASE/current.new"
mv -T "$BASE/current.new" "$BASE/current"

echo "== systemd unit (installed, not enabled, not started)"
install -m 644 "$RELEASE/deploy/company-screener.service" "$UNIT"
systemctl daemon-reload
systemd-analyze verify "$UNIT"

echo "== checks"
# Settings are validated with the real env file (as root, which can read it); the app
# is imported as the service user, from the read-only release.
(
    set -a
    # shellcheck disable=SC1091
    . "$ETC/screener.env"
    set +a
    cd /
    PYTHONPATH="$BASE/current/src" "$BASE/current/venv/bin/python" -c '
from screener.config import Settings
s = Settings(_env_file=None)
assert s.llm_backend == "mock" and s.public_demo and s.access_token, "unsafe settings"
assert s.anthropic_api_key is None, "no API key in mock mode"
print("settings ok:", s.llm_backend, "max_urls", s.max_urls_per_request,
      "active", s.max_active_runs, "concurrency", s.concurrency,
      "fetch_max_bytes", s.fetch_max_bytes)
'
)
cd /  # the service user cannot read root's working directory
runuser -u screener -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$BASE/current/src" \
    "$BASE/current/venv/bin/python" -c '
from screener.examples import load_examples
import screener.web
print("import ok; examples:", len(load_examples()))
'
echo "service enabled: $(systemctl is-enabled company-screener 2>/dev/null || true)"
echo "service active:  $(systemctl is-active company-screener 2>/dev/null || true)"
echo "Done. Nothing was activated: no DNS change, service not started, Caddy untouched."
