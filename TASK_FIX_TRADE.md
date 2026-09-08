# TASK: Фикс торговли по данным 107 сделок (06–08.09.2026)

Ветка: v.12.7_08sep (от origin/v.12.7_08sep).
Живой API: http://185.78.76.248:8000

## 0. Принцип
Меньше сделок = лучше. Система умеет зарабатывать (TP 13/13, +27.95), но пропускает
45 мёртвых входов (SL 0/45, MFE=0) и отдаёт комиссию трейлингом/рестартами.
Цель: сократить SL вдвое-втрое, поднять WR с 15% до ≥40%, выйти в плюс.

## 1. Данные (107 сделок, 06–08.09)

### 1.1 Экономика выходов
| Выход           | Сделок | WR   | Net      | Gross   | Комиссии | Вердикт |
|-----------------|--------|------|----------|---------|----------|---------|
| TAKE_PROFIT     | 13     | 100% | +27.95   | +31.26  | 3.31     | ✅ единственная прибыльная ветка |
| STOP_LOSS       | 45     | 0%   | −61.76   | −51.14  | 10.61    | ❌ конвейер слива |
| TRAILING_STOP   | 30     | 3%   | −1.55    | +4.88   | 6.43     | ❌ комиссия > gross |
| SHUTDOWN_CLOSE  | 17     | 12%  | −6.36    | −1.28   | 5.08     | ❌ рестарты убивают |
| TIME_STOP       | 2      | 0%   | −1.28    | −0.79   | 0.49     | нейтрально |

Средний выигрыш: 2.15, средний проигрыш: 1.37.
Нужный WR для безубыточности: 1.37/(2.15+1.37) = 39%. Фактический: 15%.

### 1.2 Стороны
- Sell: 72 сделки, gross −21.40
- Buy: 35 сделок, gross +4.33
Утренний Sell-лок заставил бота воевать с разворотом рынка.

### 1.3 Часы (самые токсичные)
- 10:00: 8 сделок, net −6.93
- 11:00: 8 сделок, net −2.58
- 16:00: 18 сделок, net −7.83
- 17:00: 22 сделки, net −11.73

### 1.4 Критический баг: группировка по exit_reason
На скринах: AKEUSDT active=TAKE_PROFIT, аналитика "TRAILING_STOP: 10сд".
`perf_by_strategy` группирует по exit_reason, а не по колонке strategy.
Адаптер гоняется за "TAKE_PROFIT" как за стратегией, лучшая пара заморожена.

## 2. Фиксы (приоритет 1–8)

### 2.1 Фикс группировки стратегий (КРИТИЧНО)

**analyzer.py:**
```python
def perf_by_strategy(rows):
    d={}
    for r in rows:
        strat = r[8] or "?"  # r[8]=strategy, НЕ r[2]=exit_reason
        d.setdefault(strat, []).append(r)
    return {s:trade_metrics(rs) for s,rs in d.items()}

def strategy_scores(rows, min_n=3):
    out={}
    VALID={"WALL","TREND","SWING","GRID","BREAKOUT"}
    for s,pm in perf_by_strategy(rows).items():
        if s not in VALID: continue  # whitelist
        if pm["n"]==0: continue
        net_list = [r[0] for r in rows if r[8] == s]
        std = statistics.stdev(net_list) if len(net_list) > 1 else 1.0
        sharpe = pm["net"] / std if std > 0 else pm["net"]
        out[s]={"n":pm["n"],"net":pm["net"],"net_per_trade":round(pm["net"]/pm["n"],4),"sharpe":round(sharpe,3)}
    return out
```

**adapter.py:**
```python
# После decide_strategy:
if strat not in ("WALL","TREND","SWING","GRID","BREAKOUT","OFF"):
    reason=f"invalid strategy '{strat}' (exit_reason leak) -> TREND | "+reason
    strat="TREND"
```

**Юнит-тест:**
```python
def test_strategy_grouping():
    import analyzer
    db=sqlite3.connect('market_data.db')
    rows=analyzer.get_rows(db,'AKEUSDT',240)
    scores=analyzer.strategy_scores(rows)
    VALID={"WALL","TREND","SWING","GRID","BREAKOUT"}
    assert set(scores.keys()).issubset(VALID), f"keys={scores.keys()}"
```

### 2.2 Fee-aware трейлинг

**server.py manage_position:**
```python
# Не активировать трейлинг, пока профит < round-trip fee ×1.5
round_trip_fee = CONFIG["commission_maker"] + CONFIG["commission_taker"]  # 0.00136
min_trail_profit = round_trip_fee * 1.5  # 0.00204 (0.2%)

if tact<100 and prof>=tact and atr>0 and prof>=min_trail_profit:
    td=max(px*tmin,atr*tmult)
    cand=round_price(px-td) if side=="Buy" else round_price(px+td)
    pos["sl"]=max(pos["sl"],cand) if side=="Buy" else min(pos["sl"],cand)
```

**Дефолты:**
```python
"trail_activation_pct": 0.01,  # было ниже
"trail_min_pct": 0.006,
```

### 2.3 Graceful shutdown (персист позиций)

**server.py lifespan:**
```python
# Перед shutdown: сохранить позиции в market_data.db
def save_open_positions():
    if not state.db_conn: return
    cur = state.db_conn.cursor()
    cur.execute("DELETE FROM open_positions")
    for key, pos in state.open_positions.items():
        cur.execute("""INSERT INTO open_positions(key, symbol, side, entry_price, qty,
            timestamp, atr, sl, tp, strategy, margin_used, notional, entry_commission,
            wall_price, wall_age, entry_trend, time_stop, highest, mfe, mae,
            spread_pct, atr_pct, imbalance, mtf5, mtf15)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, pos["symbol"], pos["side"], pos["entry_price"], pos["qty"],
             pos["timestamp"], pos["atr"], pos["sl"], pos["tp"], pos["strategy"],
             pos["margin_used"], pos["notional"], pos["entry_commission"],
             pos["wall_price"], pos["wall_age"], pos["entry_trend"],
             pos["time_stop"], pos["highest"], pos["mfe"], pos["mae"],
             pos["spread_pct"], pos["atr_pct"], pos["imbalance"],
             pos["mtf5"], pos["mtf15"]))
    state.db_conn.commit()

# После startup: восстановить позиции
def restore_open_positions():
    if not state.db_conn: return
    cur = state.db_conn.cursor()
    cur.execute("SELECT * FROM open_positions")
    for row in cur.fetchall():
        key = row[0]
        state.open_positions[key] = {
            "symbol": row[1], "side": row[2], "entry_price": row[3], "qty": row[4],
            "timestamp": row[5], "atr": row[6], "sl": row[7], "tp": row[8],
            "strategy": row[9], "margin_used": row[10], "notional": row[11],
            "entry_commission": row[12], "wall_price": row[13], "wall_age": row[14],
            "entry_trend": row[15], "time_stop": row[16], "highest": row[17],
            "mfe": row[18], "mae": row[19], "spread_pct": row[20], "atr_pct": row[21],
            "imbalance": row[22], "mtf5": row[23], "mtf15": row[24]
        }
    logger.info(f"✅ Восстановлено {len(state.open_positions)} позиций")
```

**init_db:**
```python
c.execute("""CREATE TABLE IF NOT EXISTS open_positions(key TEXT PRIMARY KEY,
    symbol TEXT, side TEXT, entry_price REAL, qty REAL, timestamp REAL, atr REAL,
    sl REAL, tp REAL, strategy TEXT, margin_used REAL, notional REAL,
    entry_commission REAL, wall_price REAL, wall_age REAL, entry_trend TEXT,
    time_stop REAL, highest REAL, mfe REAL, mae REAL, spread_pct REAL,
    atr_pct REAL, imbalance REAL, mtf5 TEXT, mtf15 TEXT)""")
```

**shutdown_cleanup:**
```python
def shutdown_cleanup():
    save_open_positions()  # персист перед закрытием
    # НЕ закрывать позиции taker'ом
```

### 2.4 Лимит сделок и SL-бюджет

**server.py:**
```python
# В CONFIG:
"max_daily_trades": 20,
"sl_budget_ratio": 0.64,  # SL <= 0.64 * TP за 72ч

# В entry_allowed:
def entry_allowed(symbol):
    # ... существующие проверки ...
    
    # SL-бюджет
    now = time.time()
    t0 = now - 72*3600
    state.db_cursor.execute("""SELECT exit_reason FROM trades
        WHERE timestamp>=?""", (t0,))
    reasons = [r[0] for r in state.db_cursor.fetchall()]
    tp_count = sum(1 for r in reasons if r=="TAKE_PROFIT")
    sl_count = sum(1 for r in reasons if r=="STOP_LOSS")
    sl_budget = int(tp_count * CONFIG["sl_budget_ratio"])
    
    if sl_count > sl_budget + 5:  # превышение на 5+
        log_warn_throttle(symbol, "sl_budget",
            f"SL {sl_count}/{sl_budget} за 72ч — авто-ужесточение", 300)
        # Временно увеличить фильтры
        CONFIG["_auto_tight"] = time.time()
    
    # Дневной лимит
    state.db_cursor.execute("""SELECT COUNT(*) FROM trades
        WHERE timestamp>=?""", (now - 86400,))
    daily = state.db_cursor.fetchone()[0]
    if daily >= CONFIG["max_daily_trades"]:
        return False
    
    return True
```

### 2.5 Сторона по перформансу

**adapter.py eval:**
```python
# После save_knowledge:
# Сторона по перформансу: Buy n>=3 и buy_gross<=0 и sell_gross>0 -> allowed_side=Sell
buy_gross = m.get("buy_gross", 0)
sell_gross = m.get("sell_gross", 0)
buy_n = len([r for r in rows if r[3]=="Buy"])
sell_n = len([r for r in rows if r[3]=="Sell"])

desired_side = "BOTH"
if buy_n >= 3 and buy_gross <= 0 and sell_gross > 0:
    desired_side = "Sell"
elif sell_n >= 3 and sell_gross <= 0 and buy_gross > 0:
    desired_side = "Buy"

cur_side = ov.get("allowed_side", "BOTH")
if desired_side != cur_side:
    # Кулдаун 6ч
    self.kn_cur.execute("SELECT ts FROM adaptive_log WHERE symbol=? AND param='allowed_side' ORDER BY ts DESC LIMIT 1", (symbol,))
    last_row = self.kn_cur.fetchone()
    last_ts = float(last_row[0]) if last_row else 0
    if time.time() - last_ts >= 6*3600:
        old = cur_side
        ov["allowed_side"] = desired_side
        self.log(symbol, "allowed_side", old, desired_side,
                 f"performance: buy_gross={buy_gross:.2f}, sell_gross={sell_gross:.2f}")
        self.bump(f"side {symbol}")
        self.config_api._save()
```

### 2.6 Консервативные дефолты входов

**CONFIG:**
```python
"mtf_min_confirms": 2,  # было 1
"imbalance_confirmation_ticks": 6,  # было 3
"loss_cooldown_seconds": 300,  # 5 мин после убытка
"trend_sl_atr_mult": 2.5,  # стоп дальше от входа
"min_sl_distance_pct": 0.004,  # 0.4% минимум
"require_trend_alignment": True,
```

### 2.7 Часовой блэклист

**adapter.py run (вместо 6ч → 1ч):**
```python
if time.time() - self.last_hour_adapt > 3600:  # раз в 1ч
    self.last_hour_adapt = time.time()
    for sym in self.CONFIG.get("symbols",[]):
        ov = self.CONFIG.setdefault("pair_overrides",{}).get(sym,{})
        if ov.get("locked"): continue
        rows = analyzer.get_rows(self.state.db_conn, sym, 72)
        hours = analyzer.hour_stats(rows, worst=10)  # топ-10 вместо 5
        toxic_hours = [h["h"] for h in hours if h["net"] < -0.5 and h["n"] >= 3]
        
        current_blacklist = ov.get("trading_hours_blacklist", [])
        new_blacklist = list(set(current_blacklist + toxic_hours))
        
        # Снятие часов: net > 0 за последние 24ч
        for h in list(new_blacklist):
            # Проверить последние 24ч для этого часа
            now = time.time()
            self.state.db_cursor.execute("""SELECT pnl FROM trades
                WHERE symbol=? AND timestamp>=? AND strftime('%H', datetime(timestamp, 'unixepoch'))=?""",
                (sym, now - 86400, str(h)))
            hour_pnl = sum(r[0] for r in self.state.db_cursor.fetchall())
            if hour_pnl > 0:
                new_blacklist.remove(h)
        
        if new_blacklist != current_blacklist:
            ov["trading_hours_blacklist"] = new_blacklist
            self.log(sym, "trading_hours_blacklist", current_blacklist, new_blacklist,
                     f"часовая адаптация: +{toxic_hours}, сняты с net>0")
            self.bump(f"часы {sym}")
            self.config_api._save()
```

### 2.8 Наблюдаемость

**server.py /api/status:**
```python
@app.get("/api/status")
async def get_status():
    # ... существующий код ...
    
    # SL-бюджет
    now = time.time()
    t0 = now - 72*3600
    state.db_cursor.execute("""SELECT exit_reason FROM trades WHERE timestamp>=?""", (t0,))
    reasons = [r[0] for r in state.db_cursor.fetchall()]
    tp_count = sum(1 for r in reasons if r=="TAKE_PROFIT")
    sl_count = sum(1 for r in reasons if r=="STOP_LOSS")
    sl_budget = int(tp_count * CONFIG["sl_budget_ratio"])
    
    stats["sl_budget"] = {"tp": tp_count, "sl": sl_count, "budget": sl_budget}
    
    return {"is_trading":state.is_trading, "symbols":CONFIG["symbols"],
            "ws_connected":state.ws_connected, "stats":stats, "health":health,
            "version":VERSION}
```

**UI dashboard (web_ui.html):**
```html
<div class="exit-stats">
  <h3>Причины выхода (72ч)</h3>
  <div class="exit-row">
    <span>TP: {{stats.tp}}</span>
    <span>SL: {{stats.sl}} / {{stats.sl_budget}} (бюджет)</span>
    <span>Trailing: {{stats.trailing}}</span>
  </div>
</div>
```

## 3. Критерии приёмки (24ч прогон)

- [ ] Группировка по strategy: `strategy_scores` ключи ⊆ {WALL,TREND,SWING,GRID,BREAKOUT}
- [ ] AKEUSDT active=TREND (не TAKE_PROFIT)
- [ ] STOP_LOSS <= 20 за сутки (было 45)
- [ ] Winrate >= 30% (было 15%)
- [ ] Net PnL >= −10 (было −43)
- [ ] SHUTDOWN_CLOSE = 0 (позиции персистятся)
- [ ] TRAILING_STOP gross > net + commission (комиссия не съедает)
- [ ] TAKE_PROFIT winrate 100% (эталон не сломан)

## 4. Порядок работы (PR в v.12.7_08sep)

PR1: фикс группировки 2.1 + whitelist + юнит-тест
PR2: graceful shutdown 2.3 + персист позиций
PR3: fee-aware трейлинг 2.2 + консервативные дефолты 2.6
PR4: SL-бюджет 2.4 + лимит сделок + сторона 2.5 + часы 2.7 + наблюдаемость 2.8

## 5. Проверка

```bash
# 1) Группировка
python3 -c "
import sqlite3, analyzer
db=sqlite3.connect('market_data.db')
rows=analyzer.get_rows(db,'AKEUSDT',240)
scores=analyzer.strategy_scores(rows)
print('keys:',sorted(scores.keys()))
VALID={'WALL','TREND','SWING','GRID','BREAKOUT'}
assert set(scores.keys()).issubset(VALID), f'FAIL: {scores.keys()}'
print('✅ grouping OK')"

# 2) Персист: рестарт не закрывает позиции
systemctl restart bybit-scalper.service
sleep 5
curl -s localhost:8000/api/status | python3 -c "import sys,json;print('open_positions:',len(json.load(sys.stdin)['stats']['open_positions']))"

# 3) SL-бюджет
curl -s localhost:8000/api/status | python3 -c "import sys,json;s=json.load(sys.stdin)['stats'];print('SL budget:',s.get('sl_budget',{}))"

# 4) 24ч прогон
sleep 86400
curl -s localhost:8000/api/trades/export > trades_export_fixed.csv
curl -s localhost:8000/api/stats/export > stats_export_fixed.csv
```

## 6. Не трогать

- Логику TP/SL в manage_position (эталон 13/13 TP)
- WS-payload ticker_update и REST-контракты (только добавления)
- Миграции БД: только ALTER ADD / CREATE TABLE
- Адаптивные правила v13 (уже работают)

## 7. Эталон (не сломать)

**TAKE_PROFIT (06–08.09):** 13 сделок, 100% WR, +27.95 net, 31.26 gross, 3.31 комиссии.
Это единственная прибыльная ветка — не трогать TP-логику.

**Цель:** сократить SL с 45 до <=20, поднять WR с 15% до >=40%, выйти в Net >= 0.
