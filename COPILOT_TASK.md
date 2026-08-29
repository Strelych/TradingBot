cd /root/bybit_scalper
cat > COPILOT_TASK.md <<'MDEOF'
# 🐞 Задача для Copilot: бот v12 не открывает позиции и не ставит лимитные ордера

## Контекст проекта
- Репозиторий: github.com/Strelych/TradingBot, ветка `v12-test`
- Сервер: VPS, systemd unit `bybit-scalper.service`
- Версия кода: **v12** (server.py и web_ui.html)
- Режим: paper-trading (виртуальный баланс $500, плечо 10), без API-ключей биржи
- Данные: публичные WS Bybit (orderbook.50 + publicTrade), REST kline/instruments

## Файлы
- `server.py` — цикл анализа (0.2с), входы/выходы, REST/WS, хост UI, readiness-индикатор, hot add/remove пар
- `adapter.py` — адаптер: knowledge.db, решения по стратегии и risk_mult
- `analyzer.py` — trade_metrics, regime, breakout_state, hour_stats
- `config_api.py` — runtime-конфиг с метаданными
- `web_ui.html` — Web UI (Торговля/История/Аналитика/Адаптация/Настройки)

## Стратегии и выходы
WALL / TREND / SWING / GRID / BREAKOUT. Выходы: STOP_LOSS, TRAILING_STOP, TAKE_PROFIT, TIME_STOP.
Комиссии: maker 0.036%, taker 0.1% (round-trip ~0.136%).

---

## 🚨 Текущий симптом
Бот запущен, подписан на 8 пар, торговля включена, НО:
- ❌ нет ни одной строки `📋 LIMIT` в логах
- ❌ нет ни одной строки `✅ FILL`
- ❌ нет warning-логов про пропуск по размеру (`qty < min_qty`)
- ✅ WS подключён, адаптер пишет версии конфига

## Снимок состояния (API, 28.08.2026)
### /api/pairs (все пары)
```json
{
  "BTCUSDT": {"set":"AUTO","active":"WALL","regime":{"vol_rel":2.13,"atr_pct":0.00105},
              "overrides":{"adapter_strategy":"WALL","risk_mult":0.5}},
  "SOLUSDT": {"set":"AUTO","active":"WALL","regime":{"wall_share":0.9,"atr_pct":0.00183}},
  "HUSDT":   {"set":"AUTO","active":"WALL","regime":{"atr_pct":0.00454}},
  "GRAMUSDT":{"set":"AUTO","active":"SWING"}
}
```

### /api/adapter
Все пары: `adapter_strategy=WALL` (GRAM — SWING), `risk_mult=0.5`,
reason = «мало данных: режимная канарейка ×0.5».

### Readiness-блокеры (из UI)
- BTC/ETH/HUSDT: «нет подтверждённой стены» (✗ стена)
- SOL/XRP: «ждём дисбаланс» (✗ дисбаланс)
- GRAM: «широкий спред» (✗ спред)

### Статистика последних 585 сделок (для контекста)
- Net −53.78, Gross +24.93, Комиссии 78.71 (3× от gross)
- STOP_LOSS: 231 сделка, 0 побед, −116.14
- TRAILING_STOP: 345 сделок, +63.96 (единственный плюс-канал)
- Пары: BTC −34.69, SOL −19.17, ETH +1.05, HUSDT −0.98

---

## 🎯 Гипотезы (по приоритету)
1. **Gate/readiness молча блокирует вход.** В v12 при `gate=False` функции `entry_*` не вызываются и логов нет. В FLAT-рынке WALL требует стену+дисбаланс+тренд — условия не собираются часами.
2. **Адаптер на холодном старте пиннит WALL** (рекомендация режима), а не TREND, поэтому нет входов даже при тренде.
3. **Fee-floor `min_atr_pct_abs`** отсекает мажоры (ATR 0.10–0.18% < порога), но не должен резать HUSDT (0.45%).
4. **В v12 могли пропасть warning-логи** при `qty < min_qty` и при skip — входы молча проваливаются.
5. **Размер позиции**: канарейка ×0.5 + margin_pct 0.05 → маржа ~$12 → на BTC qty ~0.0008 < min_qty 0.001 → молча `return out`.

## ✅ Требуемые действия
### Шаг 1. Диагностика (без правок)
```bash
curl -s localhost:8000/api/pairs | python3 -m json.tool
curl -s localhost:8000/api/adapter | python3 -m json.tool
curl -s localhost:8000/api/config | python3 -m json.tool
journalctl -u bybit-scalper.service --since "5 min ago" --no-pager
```

### Шаг 2. Вернуть видимость причин (правки кода, PR в v12-test)
1. Во ВСЕ `entry_*` добавить warning при `qty < min_qty` и при каждом раннем `return`.
2. В `analysis_loop` при `gate=False` писать `logger.debug(f"{symbol} gate blocked: ...")` с причиной.
3. Добавить чип «размер» в readiness (оценка ожидаемого qty против min_qty).

### Шаг 3. Логика (PR)
4. Адаптер: на холодном старте при высоком ATR (HUSDT/SOL) выбирать TREND, а не WALL; добавить гистерезис (смена стратегии только после 3 циклов подряд).
5. Не пиннить WALL в FLAT-рынке без стен — fallback в режим ожидания без ложных «готов».

## Критерии приёмки
- [ ] В логах видны `📋 LIMIT` или явные warning/`gate blocked` с причиной
- [ ] На HUSDT (принудительный TREND) за 10 минут есть ≥1 `📋 LIMIT` или warning
- [ ] Адаптер не флиппует чаще 1 раза в час
- [ ] Readiness не показывает ложное «✅ готов» при блокировке

## 🚫 Не трогать
- Формат WS-payload `ticker_update`
- REST `/api/*` (кроме расширения метаданных)
- Структуру `CONFIG_META` (только расширение)
- Миграции БД — только `ALTER ... ADD COLUMN`

## 📎 Файлы для изучения в первую очередь
1. `server.py`: `analysis_loop()`, блок `gate=...`, вызовы `entry_*`, сборка readiness
2. `adapter.py`: `decide_strategy()`, `publish_regime()`
3. `analyzer.py`: `regime()`, `recommend()` — пороги vol_rel/trendiness/wall_share
MDEOF

git add COPILOT_TASK.md
git commit -m "docs: задача для Copilot — бот v12 не открывает позиции"
git push origin v12-test
echo "✅ COPILOT_TASK.md создан и запушен"