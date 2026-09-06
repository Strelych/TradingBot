# ЗАДАЧА v2: адаптивная развязка ограничений (defaults unlocked, всё адаптивно)

Ветка: новая v12-AA (PR в неё).
Живой API для фактов: http://185.78.76.248:8000
(/api/status, /api/pairs, /api/adapter, /api/trades?limit=N, /api/config)

## 0. Принципы новой архитектуры
1. Дефолты развязаны: locked=false, allowed_side=BOTH, risk_mult in [0.25, 1.0].
   Любое сужение — решение адаптера по данным, залогировано, обратимо.
2. Ограничение = функция данных (ATR vs fee, min_qty, wall_share, тренд 1h/4h,
   перцентиль дисбаланса), а не константа.
3. OFF только через split-валидацию: обе половины окна (halves()) имеют
   net_per_trade < -0.03. Других путей к risk_mult=0 НЕТ.
4. Гарантия разведки (анти-deadlock): после каждого eval, если ВСЕ пары в
   OFF/skip_size — fee-positive пара с макс. atr_pct принудительно ставится в
   канарейку (size-валидированную). «нет данных -> нет сделок -> нет данных»
   невозможно.
5. Никаких тихих скипов и log-спама: каждый скип логируется <= 1 раза в 60с
   на (symbol, reason).

## 1. Факты (что произошло после TASK v1)

### Эталон (28.08, ручная конфигурация — РАБОТАЕТ)
HUSDT: strategy=TREND, risk_mult=1.0, allowed_side=Sell (медвежий рынок).
9 сделок, WR 55.6%, Net +3.32, Gross +4.93, комиссии 1.61 (33% от gross).
TAKE_PROFIT 4/4 = +4.76; STOP_LOSS 2 = -1.25.
Вывод: TREND на паре с ATR >= 0.2% + полный размер + TP = рабочая связка.

### Новые блокеры (логи 29.08)
- HUSDT entry_trend skipped: side not allowed (need Buy)
  -> allowed_side=Sell остался от медвежьего рынка; рынок развернулся в BULLISH.
  Ручное ограничение устарело. Должно стать адаптивным (п.2.2).
- BTCUSDT skip_size: qty 0.0 < min_qty 0.001 even after bump to 0.075
  -> size-bump сделан как разовый x1.5, не решает уравнение (п.2.3);
  и BTC (ATR 0.10% < fee_floor) вообще не должен пытаться входить (п.2.4).
- SOLUSDT no imbalance / AKEUSDT no side — нормальное ожидание, НЕ баг.
- warnings сыпятся каждый тик (0.2с) — log-спам (п.2.5).

### Баги адаптера (из /api/adapter)
- Обнуляет risk_mult до 0.0 на locked-парах (HUSDT: locked=true, risk 0.0
  до ручной правки).
- Флип-флоп WALL<->SWING каждые ~15 мин (хистерезис 1/3-2/3 не держит).
- В FLAT хочет OFF для пар с wall_share>0.7 («FLAT без стен»).

## 2. Требуемые изменения

### 2.1 Дефолты (CONFIG_META + adapter)
- allowed_side дефолт BOTH; locked дефолт false; risk_mult дефолт 1.0.
- locked=true: адаптер НЕ меняет strategy; risk_mult может менять ТОЛЬКО через
  size-валидацию (2.3) в диапазоне [0.25, 1.0]. Для полной заморозки — новый
  флаг locked_strict=true (дефолт false).

### 2.2 Адаптивный allowed_side (вместо ручного Sell/Buy)
- side_bias по старшему тренду: тренд 1h BEARISH держится >= 3 свечей -> Sell;
  BULLISH >= 3 свечей -> Buy; иначе BOTH.
- Смена стороны не чаще 1 раза в 6ч (хистерезис), запись в adaptive_log.
- Разворот тренда 1h -> автосброс в BOTH в течение 1 цикла eval.

### 2.3 Size-bump: решать уравнение, не итерировать
Порядок: сначала fee-фильтр (2.4), ПОТОМ размер.

    required_notional = min_qty * price * 1.2
    required_rm = required_notional / (balance * margin_pct * leverage)
    risk_mult = min(1.0, max(current_rm, required_rm))
    if required_rm > 1.0 -> статус skip_size (лог + чип readiness), сделок нет

Никаких итераций x1.5. Пересчёт на каждом eval (60с), не на каждом тике.

### 2.4 Fee-отрицательные пары: не пытаться
- atr_pct < min_atr_pct_abs (fee_floor=0.0016) -> TREND не предлагается;
  пара OFF (WALL с maker-выходами — только как будущая возможность).
- BTC/ETH сейчас попадают сюда -> OFF по умолчанию, без skip_size попыток.

### 2.5 Троттлинг логов
- dict last_log[(symbol, reason)] = ts; warning пишется только если
  now - ts >= 60. Распространить на все скипы entry_* и gate blocked.

### 2.6 Хистерезис без флип-флопа
- Смена стратегии только после 3 циклов подряд с новой рекомендацией.
- В период накопления держим текущую; config_versions пишем ТОЛЬКО при
  фактическом переключении.
- Смена risk_mult не чаще 1 раза в 6ч на пару (кроме 2.3).

### 2.7 Гарантия разведки
- После eval: если ни одна пара не в торговом режиме (все OFF/skip_size) —
  выбрать fee-positive пару с макс. atr_pct -> канарейка TREND 0.5
  (size-валидированная), лог «разведка анти-deadlock».

### 2.8 Мягкие статические дефолты (пока не адаптивные)
- imbalance_threshold 0.5 (было 0.6-0.7)
- wall_volume_multiplier 6 (было 8)
- mtf_min_confirms 1 (было 2-3)
- margin_pct 0.1 (было 0.05) — канарейки чаще проходят min_qty
- trading_hours_blacklist [] по умолчанию. НЕ хардкодить часы: адаптер сам
  копит hour_stats и предлагает блэклист только со split-валидацией
  (исторически прибыльные 05,07,08; токсичные 01,03,09,10,11,13,15-23).

## 3. Критерии приёмки
- [ ] HUSDT: нет «side not allowed» при развороте; allowed_side сам Sell->BOTH/Buy
- [ ] BTC/ETH: OFF, в логах нет skip_size попыток
- [ ] В adaptive_log нет risk_mult=0 без причины split-валидации
- [ ] locked-пара: strategy не тронута; risk_mult только через 2.3
- [ ] Логи: <= 1 warning в 60с на (symbol, reason)
- [ ] Свежая БД: >= 1 вход за 2 часа на fee-positive паре (разведка работает)
- [ ] <= 1 смены стратегии на пару за 6ч (нет флип-флопа)
- [ ] config_versions: <= 1 записи на фактическое переключение

## 4. Порядок работы (отдельные PR)
1. PR1: дефолты + адаптивный allowed_side + троттлинг логов (2.1, 2.2, 2.5)
2. PR2: уравнение размера + fee-отрицательные OFF (2.3, 2.4)
3. PR3: хистерезис + разведка + мягкие дефолты (2.6, 2.7, 2.8)
После каждого PR: деплой (git pull + systemctl restart bybit-scalper),
наблюдение 1-2ч, контроль /api/analytics + journalctl.

## 5. Проверка
    systemctl restart bybit-scalper.service
    journalctl -u bybit-scalper.service -f | grep -E "LIMIT|FILL|WARNING"
    curl -s localhost:8000/api/adapter | python3 -m json.tool
    curl -s localhost:8000/api/pairs   | python3 -m json.tool

## 6. Не трогать
- Формат WS-payload ticker_update и REST /api/* контракты
- CONFIG_META только расширять; миграции БД только ALTER TABLE ... ADD COLUMN
- Рабочую связку TREND+TP на fee-positive парах (эталон 28.08)