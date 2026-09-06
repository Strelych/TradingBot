# TASK v0.13.0: консолидация и релиз (ветка УЖЕ создана)

ВАЖНО: ветка v0.13.0 уже создана владельцем и содержит актуальное состояние
VPS (v12.7 + все хотфиксы). Ветку НЕ создавать — работать в существующей.
Живой API: http://185.78.76.248:8000

## 0. Цель
Одна активная ветка v0.13.0. Все строки версии = v0.13.0.
Бот автономный: сам адаптирует стопы/входы/часы/стороны/пары, не душит себя.
Старые ветки (v12.x, v12-test, v13-adapter) — в архив после merge.

## 1. Данные-ориентиры
Эталон (28.08): 9 сделок, WR 55.6%, Net +3.32, TP 4/4 = +4.76, комиссии 33% gross.
v12.7 (06.09): 32 сделки, WR 12.5%, Net -13.47; STOP_LOSS 0/18 = -17.17;
TP 4/4 = +6.09; Buy 0/3; часы 10-13 = -13.26; HUSDT 0/11; AKE 4/21.
Цель v0.13.0: WR >= 40%, Net >= 0, SL <= 10/сутки, TP winrate 100%.

## 2. Что УЖЕ в базе v0.13.0 (не ломать, покрыть тестами)
Подтверждено боевыми логами и adaptive_log:
- REST-observability (health.rest_ok), валидация bybit_base_url, autostart
- lazy init http-сессии; фиксы sym_info[1], orderbooks max/min, баланс из stats
- одна канарейка режима; canary_fraction только для пограничных rm=0.25
- адаптивные правила PR1 + кулдауны 6ч + нормализация кортежей 3->4 + всеядный
  потребитель + traceback в run()
- РАБОТАЕТ в бою (записи adaptive_log):
  AKE trend_sl_atr_mult 1.5->1.875 ("SL 0/22 побед")
  AKE trend_trail_atr_mult 2.0->2.4 ("trailing gross>0 net<=0")
  часовая адаптация (AKE [10,14,15,16], HUSDT [11,12,14,15], SOL [16,17])
  авто-ребаланс 24ч (HUSDT OFF при 10/10 убыточных)
  risk_mult 0.5->1.0 по Sharpe (+24.95, n=10)

## 3. Версия v0.13.0 во всех местах
- server.py: VERSION="v0.13.0"; BRANCH="v0.13.0"
- /api/version и /api/status отдают v0.13.0; UI читает версию ТОЛЬКО из API
- deploy/bybit-scalper.service: Description=Bybit Scalper v0.13.0
- README.md: заголовок v0.13.0 + таблица статуса веток
- после merge: git tag v0.13.0 && git push origin v0.13.0 --tags

## 4. Что ДОБАВИТЬ в v0.13.0

### 4.1 Автоподбор пар (pair_selector.py, из TASK_v12.8)
- скан Bybit instruments-info + tickers раз в 6ч; кнопка "Сканировать сейчас"
- скоринг: 0.4*ATR + 0.3*объём + 0.2*спред(inv) + 0.1*возраст (min-max)
- фильтры: volume24h >= $500K, возраст >= 7д, ATR >= fee_floor, спред <= 0.1%
- whitelist HUSDT/AKEUSDT/STXUSDT; blacklist BTCUSDT/ETHUSDT
- лимит 3..10 пар; apply_symbols() без рестарта; логи в adaptive_log
- таблица pair_scans; endpoints /api/scan-pairs, /api/pair-candidates
- UI-вкладка "Пары": активные / кандидаты (score,ATR,vol) / blacklist

### 4.2 Консервативные дефолты (из TASK_v12.9)
    trend_sl_atr_mult 2.5; min_sl_distance_pct 0.004
    imbalance_confirmation_ticks 5; mtf_min_confirms 2
    loss_cooldown_seconds 300
    trading_hours_blacklist [0,1,2,3,4,5,10,11,12,13]  (адаптер расширяет/снимает)

### 4.3 Дозавершить адаптацию (из TASK_ADAPTER_V13; часть уже в базе)
- КОНТРАКТ: adaptive_rules ВСЕГДА 4-кортежи (param, old, new, reason);
  всеядный потребитель оставить как защиту; юнит-тест формата
- trade_metrics += dead_entry_rate (доля убытков с MFE < 0.002)
- сторона по перформансу: Buy n>=3 и buy_gross<=0 и sell_gross>0 ->
  allowed_side=Sell; автосброс BOTH при buy_gross>0 за следующие 10 сделок
- быстрый ребаланс пары 6ч: net<-5 и wr<20 и n>=8 -> risk_mult x0.5 (floor 0.25);
  восстановление: 3 прибыльные из 5 -> вернуть rm (старый 24ч-ребаланс оставить)
- часы: проверка раз в 1ч (в базе 6ч); снятие часа при net>0 за следующие 24ч
- /api/adapter += rules_applied_24h и last_metrics (m + perf по стратегиям)

### 4.4 UI
- версия из API; вкладки Торговля/История/Аналитика/Адаптация/Пары/Настройки
- Адаптация: last_metrics, rules_applied_24h, отсчёт кулдауна смены
- точечный DOM-update на WS (без мерцания)

### 4.5 Чистка репозитория
- .gitignore += knowledge.db, market_data.db*, *.backup, *.notfixed, backup/,
  venv/, v8/, v10*/; git rm --cached knowledge.db market_data.db
- TASK_v12.* и TASK_ADAPTER_V13.md -> docs/archive/
- удалить из рабочей директории *.notfixed, *.backup

## 5. Критерии приёмки
- [ ] origin/v0.13.0 + тег v0.13.0; README-таблица веток
- [ ] /api/version == v0.13.0; UI-шапка v0.13.0 (из API)
- [ ] pytest green: test_adaptive_rules_4tuples, test_pair_selector,
      test_config_defaults
- [ ] 24ч прогон: WR >= 40%, Net >= 0, SL <= 10, "adapter err" = 0
- [ ] adaptive_log за 24ч: >=1 запись каждого типа правила
- [ ] pair_selector добавил/удалил пару без рестарта (или обоснованно не тронул)
- [ ] TP winrate 100% (эталон не сломан)

## 6. Порядок работы (PR в v0.13.0)
PR1: версия v0.13.0 + чистка репо + README + архив TASK + тесты формата правил
PR2: pair_selector + endpoints + UI "Пары" + тесты
PR3: дефолты 4.2 + дозавершение адаптации 4.3 + тесты
PR4: UI 4.4 + релиз: tag v0.13.0, архивация старых веток

## 7. Проверка
    git push -u origin v0.13.0
    curl -s localhost:8000/api/version | python3 -m json.tool
    python3 -m pytest -q
    journalctl -u bybit-scalper.service --since "10 min ago" --no-pager | grep -cE "adapter err|Traceback"
    sqlite3 knowledge.db "SELECT symbol,param,old,new FROM adaptive_log ORDER BY id DESC LIMIT 10"

## 8. Не трогать
- WS-payload ticker_update и REST-контракты (только добавления)
- логику TP/SL в manage_position (эталон 4/4)
- миграции БД: только ALTER ADD / CREATE TABLE
- боевые правила из п.2 — только покрывать тестами и расширять