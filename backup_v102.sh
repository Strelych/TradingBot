#!/bin/bash
# backup_v102.sh — снимок текущей (v10.2) версии перед деплоем v10.4
set -e
SRC=/root/bybit_scalper
DST=/v10.2
mkdir -p "$DST"
for f in server.py analyzer.py config_api.py web_ui.py web_ui.html config_override.json; do
  [ -f "$SRC/$f" ] && cp -v "$SRC/$f" "$DST/"
done
# дамп текущей БД (на всякий случай)
sqlite3 "$SRC/market_data.db" ".backup '$DST/market_data.db'" 2>/dev/null || cp -v "$SRC/market_data.db" "$DST/"
echo "✅ Бэкап v10.2 → $DST"; ls -la "$DST"
