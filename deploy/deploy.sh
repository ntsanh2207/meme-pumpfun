#!/bin/sh
# Upload the app to the VPS and (re)start it. Usage: deploy/deploy.sh
set -eu
HOST=root@45.76.106.71
DIR=/opt/pump-graduated
cd "$(dirname "$0")/.."
ssh "$HOST" "id pumpgrad >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin pumpgrad; mkdir -p $DIR/data/settings && chown pumpgrad: $DIR/data $DIR/data/settings && chmod 750 $DIR/data/settings; T=$DIR/data/settings/admin_token; [ -s \$T ] || { openssl rand -hex 12 > \$T; chown pumpgrad: \$T; chmod 600 \$T; }"
scp -q app/collector.py app/first_trades.py app/holders.py app/watch.py app/notify.py app/trader.py app/analyze.py app/rules.py app/server.py app/index.html "$HOST:$DIR/"
scp -q deploy/pump-graduated-web.service deploy/pump-graduated-collect.service deploy/pump-graduated-collect.timer deploy/pump-graduated-analyze.service deploy/pump-graduated-analyze.timer deploy/pump-graduated-rules.service deploy/pump-graduated-rules.path deploy/pump-graduated-holders.service deploy/pump-graduated-watch.service deploy/pump-graduated-trader.service "$HOST:/etc/systemd/system/"
ssh "$HOST" "systemctl daemon-reload && systemctl enable pump-graduated-web.service pump-graduated-collect.timer pump-graduated-analyze.timer pump-graduated-rules.path pump-graduated-holders.service pump-graduated-watch.service pump-graduated-trader.service >/dev/null 2>&1 && systemctl start pump-graduated-web.service pump-graduated-analyze.timer pump-graduated-rules.path && systemctl restart pump-graduated-holders.service && systemctl restart pump-graduated-watch.service && systemctl restart pump-graduated-trader.service && systemctl restart pump-graduated-web.service && ufw allow 8090/tcp >/dev/null && systemctl is-active pump-graduated-web.service"
