# TradingBot — Bybit Scalper v11

Адаптивный скальпер Bybit Linear на публичных WS-потоках.

## Стратегии
WALL / TREND / SWING / GRID / BREAKOUT

## Файлы
- server.py — основной цикл, REST/WS, readiness-индикатор
- adapter.py — адаптер (strategy / risk_mult / canary)
- analyzer.py — метрики, regime, breakout_state, hour_stats
- config_api.py — конфиг с метаданными
- web_ui.py / web_ui.html — Web UI
- knowledge.db — накопленная память адаптера (опционально)

## Запуск
