#!/usr/bin/env bash
# Take the demo out of Caddy: remove ONLY the screener.rustwatchtower.net site block from
# the current Caddyfile, validate, then reload. Every other site stays exactly as it is now.
#
#   sudo bash caddy-detach.sh --dry-run   # show the change and validate it; change nothing
#   sudo bash caddy-detach.sh             # do it
#
# Why not restore an old backup of the whole file: it would also undo any later change to
# the other sites in it.
set -euo pipefail

CADDYFILE=/etc/caddy/Caddyfile
SITE=screener.rustwatchtower.net
DRY_RUN=false
[[ ${1:-} == --dry-run ]] && DRY_RUN=true
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 755 "$work" # the caddy user validates the new file from here

# The block: the comment lines right above it, the site line, and everything up to the
# first "}" at the start of a line. It must occur exactly once.
python3 - "$CADDYFILE" "$work/Caddyfile" "$SITE" <<'EOF'
import re
import sys

src, dst, site = sys.argv[1:]
text = open(src, encoding="utf-8").read()
block = re.compile(r"\n*(?:#[^\n]*\n)*" + re.escape(site) + r" \{\n.*?^\}\n?", re.S | re.M)
found = block.findall(text)
if len(found) != 1:
    sys.exit(f"expected exactly one {site} block, found {len(found)}; nothing changed")
open(dst, "w", encoding="utf-8").write(block.sub("\n", text, count=1))
EOF
chmod 644 "$work/Caddyfile"

echo "== change"
diff -u "$CADDYFILE" "$work/Caddyfile" || true
echo "== validate (as the caddy user: as root it can leave a root-owned log file)"
runuser -u caddy -- caddy validate --config "$work/Caddyfile" --adapter caddyfile

if $DRY_RUN; then
    echo "dry run: nothing changed"
    exit 0
fi

backup=$CADDYFILE.bak-$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$CADDYFILE" "$backup"
cat "$work/Caddyfile" >"$CADDYFILE" # same file: keeps its owner and mode
if ! systemctl reload caddy; then
    cat "$backup" >"$CADDYFILE"
    echo "reload failed; the previous Caddyfile is back in place (copy: $backup)" >&2
    exit 1
fi
echo "removed the $SITE block and reloaded Caddy; the previous file: $backup"
