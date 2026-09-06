# 🛠 Задача: переписать логику адаптера и анализатора (deadlock защиты ↔ овертрейдинг)

Ветка: `v12-test` (PR в неё). Сервер: VPS, публичный API доступен извне:
http://185.78.76.248:8000 — GET /api/status, /api/pairs, /api/adapter, /api/trades?limit=N, /api/config, /api/analytics

## 1. Архитектура (контракты НЕ ломать)
- server.py — analysis_loop (0.2с), entry_wall/trend/swing/grid/breakout, manage_position,
  readiness (checks/status_text), apply_symbols (hot pairs), housekeeping (hot-reload + publish_regime)
- analyzer.py — regime(), recommend(), breakout_state(), get_rows(), trade_metrics(), halves(),
  perf_by_strategy(), strategy_scores(), hour_stats(), decide_strategy(), adaptive_rules()
- adapter.py — Adapter: knowledge.db (adaptive_log, config_versions, strategy_performance,
  pair_knowledge, live_regime); eval() каждые 60с и по событию (10 сделок);
  пишет adapter_strategy/risk_mult в pair_overrides
- config_api.py — CONFIG_META + /api/config; web_ui.html — UI

Ограничения:
- формат WS-payload ticker_update и REST /api/* не менять
- CONFIG_META только расширять; миграции БД только ALTER TABLE ... ADD COLUMN
- pair_overrides[sym].locked=true → адаптер НЕ пишет strategy и risk_mult по паре

## 2. История проблемы (данные)

### Режим А — v10.4: овертрейдинг и комиссионный слив (20–22.08, 585 сделок)
- Net −53.78; Gross +24.93; Комиссии 78.71 (316% от gross); WR 6.0%
- STOP_LOSS: 231 сделка, 0 побед, −116.14; TRAILING_STOP: 345, +63.96 (единственный плюс)
- Пары: BTC −34.69 (WR 2.6%), SOL −19.17 (3.9%), ETH +1.05 (30%), HUSDT −0.98 (gross +14.94)
- Причина: TREND входил на мажорах с ATR 0.03–0.10%, где движение < round-trip комиссии (0.136–0.2%)

### Режим Б — v11/v12: перестраховка → простой (26–28.08, 0 сделок, до 14ч простоя)
Защитные фильтры по отдельности разумны, вместе дают deadlock:
1. Холодная БД → адаптер «мало данных: режимная канарейка ×0.5» и пиннит WALL
   (recommend() в FLAT возвращает WALL).
2. WALL в FLAT требует: стену (age≥60с, persistence≥5) + дисбаланс ≥ порога 3 тика +
   MTF-тренд — условия часами не собираются.
3. v12-хистерезис «FLAT без стен (wall_share=0) -> OFF» обнуляет risk_mult (0.0) →
   размер позиции 0 → тихий пропуск входа.
4. Канарейка ×0.5 при margin_pct 0.05 на BTC даёт qty < min_qty → тихий return out.
5. Флип-флоп: адаптер переключает WALL↔SWING каждые ~15 минут (хистерезис 1/3–2/3 не стабилизирует).
Итог: «нет данных → нет сделок → нет данных».

### Эталон — что РАБОТАЕТ (28.08, ручная конфигурация)
HUSDT: strategy=TREND (locked=true), risk_mult=1.0 → 9 сделок, WR 55.6%, Net +3.32,
Gross +4.93, комиссии 1.61 (33% от gross); TAKE_PROFIT 4/4 = +4.76.
Вывод: TREND на паре с ATR ≥ 0.2% + полный размер + TP = рабочая связка.
Эту конфигурацию НЕ ломать.

## 3. Корневая причина
decide_strategy() выбирает стратегию по истории (net_per_trade), но на холодном старте
падает в recommend(), который игнорирует fee-экономику и волатильность пары;
защитные правила (OFF по wall_share, risk_mult=0, канарейки) применяются без проверки
размера против min_qty и без гарантии «разведки».

## 4. Требуемые изменения

### 4.1 analyzer.recommend() — режим с учётом fee-экономики
fee_floor = CONFIG min_atr_pct_abs (0.0016). Новый приоритет:
- atr_pct >= fee_floor и trendiness >= 0.15 → TREND («волатильна, есть трендовость»)
- atr_pct >= fee_floor и wall_share >= 0.5 и trendiness < 0.15 → WALL
- atr_pct >= fee_floor и vol_rel < 0.7 → SWING
- atr_pct < fee_floor → класс FEE_NEGATIVE: TREND не предлагать никогда;
  WALL допустим только как канарейка; в разведке не торговать.
FLAT без стен НЕ должен по умолчанию возвращать WALL.

### 4.2 analyzer.decide_strategy() — холодный старт и честный OFF
- n < min_sample: НИКОГДА не OFF и не risk_mult=0; стратегия = recommend() из 4.1,
  риск = канарейка 0.5 с проверкой размера (4.3).
- OFF только при split-валидации: ОБЕ половины окна (halves()) имеют
  net_per_trade < off_floor (−0.03).
- Пограничный (≥ −0.03) → канарейка ×0.25 (сохранить).
- Прибыльная → ×0.5 / ×1.0 при n≥10 (сохранить).

### 4.3 Проверка размера (min_qty) — новый хелпер
expected_qty = f(balance, margin_pct, risk_mult, leverage, price); min_qty — из
instruments-info (уже есть в server.get_symbol_info).
- канарейка qty < min_qty → поднять risk_mult до min(1.0, проходного × 1.2 запаса);
- и при ×1.0 не проходит → статус пары «skip_size»: warning-лог + чип в readiness,
  сделок нет ОСОЗНАННО. Тихих пропусков быть не должно.

### 4.4 adapter.py — locked и гарантия разведки
- locked=true → не трогать strategy и risk_mult (сейчас обнуляет risk на locked HUSDT — баг).
- Гарантия разведки (анти-deadlock): после eval, если ВСЕ пары в OFF/skip —
  выбрать fee-positive пару с макс. atr_pct и поставить ей канарейку по 4.1.
- Любую установку risk_mult=0 писать в adaptive_log с причиной и данными halves().

### 4.5 Хистерезис без флип-флопа
- Смена стратегии — только после 3 циклов подряд с новой рекомендацией;
  в период накопления текущую стратегию держать, config_versions не засорять.
- Смена risk_mult — не чаще 1 раза в 6ч на пару (кроме правок размера из 4.3).

### 4.6 Здравые дефолты (CONFIG / config_override)
- imbalance_threshold 0.5 (было 0.6–0.7)
- mtf_min_confirms 1 (было 2–3)
- wall_volume_multiplier 6 (было 8)
- margin_pct 0.1 (было 0.05) — канарейки проходят min_qty
- min_atr_pct_abs 0.0016 — оставить; trading_hours_blacklist [] — оставить
- require_trend_alignment: true для WALL; для TREND достаточно своего тренда + MTF≥1

## 5. Критерии приёмки
- [ ] Свежая БД: ≥1 вход (📋 LIMIT / ✅ FILL) за 2 часа на паре с atr_pct ≥ 0.002
- [ ] Нет risk_mult=0 без split-валидации в adaptive_log
- [ ] locked-пары не изменяются адаптером (проверить pair_knowledge + log)
- [ ] ≤1 смены стратегии на пару за 6 часов (нет флип-флопа)
- [ ] Все пропуски залогированы (⚠️ min_qty / gate blocked), readiness без ложных «✅ готов»
- [ ] Ручная конфигурация HUSDT TREND ×1.0 работает после деплоя
- [ ] Комиссии/gross ≤ 50% на канареечных сделках (входят только fee-positive пары)

## 6. Проверка
    systemctl restart bybit-scalper.service
    journalctl -u bybit-scalper.service -f | grep -E "📋|✅|⚠️"
    curl -s localhost:8000/api/adapter | python3 -m json.tool
    curl -s localhost:8000/api/pairs   | python3 -m json.tool
Извне: http://185.78.76.248:8000/api/status

## 7. Порядок работы (отдельные PR)
1. PR1: recommend() + decide_strategy() (п.4.1–4.2) + юнит-тесты на синтетических rows
2. PR2: размер + locked + гарантия разведки (п.4.3–4.4)
3. PR3: хистерезис + дефолты (п.4.5–4.6)
После каждого PR: деплой (git pull + restart), наблюдение 1–2ч, контроль /api/analytics.
