# TASK v12.5: Автономный адаптивный скальпер

**Ветка:** `v12-AA` (PR в неё)  
**Контекст:** Бот работает (9 сделок HUSDT TREND, Net +$3.32, WR 55.6%), но адаптер слишком консервативен → простой в FLAT-рынке. Цель: автономная работа без ручных правок, WR ≥50%, Net > 0.

**Живой API:** http://185.78.76.248:8000

---

## 0. Принципы

1. **Автономность:** бот сам развязывает себе руки, когда данные позволяют, и сам ограничивает, когда данные запрещают.
2. **Анти-deadlock:** гарантия разведки — если все пары в OFF/skip, fee-positive пара с макс. ATR принудительно в канарейку.
3. **Адаптивность:** ограничения = функции данных (ATR, wall_share, trendiness), а не константы.
4. **Прозрачность:** каждый скип/решение залогировано, UI показывает точную причину простоя.

---

## 1. Критические баги (из аудита v12-AA)

### 1.1 `atr_pct` не передаётся в adapter

**Файл:** `analyzer.py:regime()`  
**Проблема:** `regime()` возвращает dict без `atr_pct` → `publish_regime()` записывает 0 → `decide_strategy()` видит `atr_pct=0 < fee_floor` → все пары OFF.

**Фикс:**
```python
def regime(state, symbol):
    sr=state.support_resistance.get(symbol,{})
    atr_pct=sr.get("atr_pct",0)
    hist=state.atr_hist.get(symbol,[])
    med=statistics.median(hist) if hist else (atr_pct or 0.001)
    vol_rel=atr_pct/med if med>0 else 1.0
    tot=state.tick_total.get(symbol,0); wt=state.tick_walls.get(symbol,0)
    return {"vol_rel":round(vol_rel,2),"trendiness":round(trendiness(sr.get("closes",[])),3),
            "wall_share":round(wt/tot,2) if tot>0 else 0.0,"atr_pct":round(atr_pct,5)}  # ← ДОБАВИТЬ
```

### 1.2 Split-валидация не работает

**Файл:** `adapter.py:eval()`  
**Проблема:** `eval()` не вычисляет `h1,h2=analyzer.halves(rows)` и не передаёт в `decide_strategy()` → split-валидация не работает → OFF не применяется правильно.

**Фикс:**
```python
def eval(self,symbol,ov):
    rows=analyzer.get_rows(self.state.db_conn,symbol,72)
    m=analyzer.trade_metrics(rows)
    h1,h2=analyzer.halves(rows)  # ← ДОБАВИТЬ
    scores=analyzer.strategy_scores(rows)
    # ...
    strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current,stale,m,h1,h2)  # ← ПЕРЕДАТЬ
```

### 1.3 "FEE_NEGATIVE" используется как стратегия

**Файл:** `analyzer.py:recommend()`  
**Проблема:** `recommend()` возвращает "FEE_NEGATIVE" → server.py использует как `active="FEE_NEGATIVE"` → бот не входит (нет такой стратегии).

**Фикс:**
```python
def recommend(reg):
    # ...
    else:
        return "OFF", "ATR < fee_floor — избегать торгов"  # ← OFF вместо FEE_NEGATIVE
```

### 1.4 `compute_required_rm()` не определена

**Файл:** `server.py:~150`  
**Проблема:** Функция вызывается, но не определена → `NameError` при size-bump.

**Фикс (добавить в `utils.py` или `adapter.py`):**
```python
def compute_required_rm(min_qty, price, balance, margin_pct, leverage):
    """Вычисляет минимальный risk_mult для прохождения min_qty."""
    if min_qty <= 0 or price <= 0 or balance <= 0 or margin_pct <= 0:
        return 1.0
    required_notional = min_qty * price
    required_margin = required_notional / leverage
    required_rm = required_margin / (balance * margin_pct)
    return min(1.0, required_rm)
```

### 1.5 `log_warn_throttle()` не определена

**Файл:** `server.py` (вызывается в `entry_trend`, `entry_wall`, etc.)  
**Проблема:** Функция не определена → `NameError` при первом skip.

**Фикс (добавить в `server.py`):**
```python
import time

_last_warn = {}
def log_warn_throttle(symbol, reason, message, cooldown=60):
    """Логировать warning не чаще 1 раза в cooldown секунд на (symbol, reason)."""
    key = f"{symbol}:{reason}"
    now = time.time()
    if key not in _last_warn or now - _last_warn[key] >= cooldown:
        logger.warning(message)
        _last_warn[key] = now
```

### 1.6 Двойной расчёт `calc_qty`

**Файл:** `server.py` (строки ~200 и ~350)  
**Проблема:** Второй расчёт перезаписывает `calc_qty` без учёта size-bump → readiness показывает ложное "✅ готов".

**Фикс:** Удалить второй расчёт (строки ~350), использовать `calc_qty` из первого блока (с size-bump).

### 1.7 `risk_change_cooldown` не реализован

**Файл:** `config_api.py` добавляет параметр, но `adapter.py` не использует.

**Фикс:**
```python
class Adapter:
    def __init__(self):
        # ...
        self.last_risk_change = {}  # symbol -> timestamp
    
    def eval(self,symbol,ov):
        # ...
        if not locked:
            if abs((ov.get("risk_mult") or 1.0)-rm)>1e-9:
                last_change = self.last_risk_change.get(symbol, 0)
                cooldown = self.get_param(symbol, "risk_change_cooldown") or 21600
                if time.time() - last_change >= cooldown:
                    changes.append(("risk_mult",ov.get("risk_mult"),rm,reason))
                    self.last_risk_change[symbol] = time.time()
```

---

## 2. Автономная логика (анти-deadlock)

### 2.1 Гарантия разведки

**Файл:** `adapter.py:eval()` (после цикла по парам)

Если после eval **все пары** в OFF/skip_size → выбрать fee-positive пару с макс. `atr_pct` → принудительно поставить канарейку TREND ×0.5.

```python
def run(self):
    while True:
        await asyncio.sleep(60)
        try:
            all_off = True
            best_candidate = None
            best_atr = 0
            
            for symbol in self.CONFIG["symbols"]:
                ov=self.CONFIG.setdefault("pair_overrides",{}).setdefault(symbol,{})
                self.eval(symbol,ov)
                
                # Проверяем, торгуется ли пара
                strat = ov.get("adapter_strategy") or ov.get("strategy")
                rm = ov.get("risk_mult", 0)
                if strat not in ("OFF", None) and rm > 0:
                    all_off = False
                
                # Ищем лучшего кандидата для разведки
                lr = self.read_regime(symbol)
                atr_pct = lr[3]
                if atr_pct >= 0.0008 and atr_pct > best_atr:  # fee-positive
                    best_atr = atr_pct
                    best_candidate = symbol
            
            # Если все пары OFF — принудительная разведка
            if all_off and best_candidate:
                ov = self.CONFIG["pair_overrides"][best_candidate]
                ov["strategy"] = "TREND"
                ov["risk_mult"] = 0.5
                ov["allowed_side"] = "BOTH"
                self.log(best_candidate, "strategy", ov.get("strategy"), "TREND", "гарантия разведки: все пары OFF")
                self.log(best_candidate, "risk_mult", ov.get("risk_mult"), 0.5, "гарантия разведки")
                self.bump(f"разведка {best_candidate}")
                self.config_api._save()
            
            self.kn_conn.commit()
        except Exception as e:
            print("adapter err:",e)
```

### 2.2 Адаптивный allowed_side (автосброс при развороте)

**Файл:** `adapter.py:eval()`

Если тренд 1h развернулся → сбросить `allowed_side` в BOTH (работает даже при `locked=true`).

```python
def eval(self,symbol,ov):
    # ...
    t1=self.state.support_resistance.get(symbol,{}).get("trend1")
    if t1 and self.last_trend.get(symbol) and t1!=self.last_trend.get(symbol):
        if ov.get("allowed_side") not in (None,"BOTH"):
            ov["allowed_side"]="BOTH"
            self.log(symbol,"allowed_side",ov.get("allowed_side"),"BOTH",f"разворот 1h: {self.last_trend[symbol]}->{t1}")
    self.last_trend[symbol]=t1
```

### 2.3 Авто-ребаланс пар (убыточные → OFF на 24ч)

**Файл:** `adapter.py:eval()`

Если пара убыточна 10 сделок подряд → автоматический OFF на 24ч.

```python
def eval(self,symbol,ov):
    # ...
    rows = analyzer.get_rows(self.state.db_conn, symbol, 24)  # последние 24ч
    if len(rows) >= 10:
        last_10 = sorted(rows, key=lambda r: r[5], reverse=True)[:10]  # последние 10 сделок
        losses = sum(1 for r in last_10 if r[0] <= 0)
        if losses >= 8:  # 8 из 10 убыточных
            if ov.get("adapter_strategy") != "OFF":
                ov["adapter_strategy"] = "OFF"
                ov["risk_mult"] = 0.0
                self.log(symbol, "adapter_strategy", ov.get("adapter_strategy"), "OFF", f"авто-ребаланс: 8/10 убыточных за 24ч")
                self.bump(f"авто-OFF {symbol}")
                self.config_api._save()
```

---

## 3. Режимы адаптера

### 3.1 Новый параметр `adapter_mode`

**Файл:** `config_api.py`

```python
"adapter_mode": {
    "group":"Адаптер",
    "type":"str",
    "desc":"Режим адаптера: aggressive/balanced/tight/manual",
    "default":"balanced"
},
```

**Режимы:**

| Режим | risk_mult | off_floor | min_sample | Поведение |
|-------|-----------|-----------|------------|-----------|
| **aggressive** | 1.0 | -0.01 | 10 | Быстрая разведка, высокий риск, ранний OFF |
| **balanced** | 0.5 | -0.03 | 20 | Сбалансированный (дефолт) |
| **tight** | 0.25 | -0.05 | 30 | Консервативный, долгая разведка, поздний OFF |
| **manual** | - | - | - | Адаптер выключен, только ручные настройки |

### 3.2 Логика режимов

**Файл:** `adapter.py:eval()`

```python
def eval(self,symbol,ov):
    mode = self.get_param(symbol, "adapter_mode") or "balanced"
    
    if mode == "manual":
        # Адаптер выключен, не трогаем настройки
        return
    
    # Параметры режима
    if mode == "aggressive":
        default_rm = 1.0
        off_floor = -0.01
        min_sample = 10
    elif mode == "tight":
        default_rm = 0.25
        off_floor = -0.05
        min_sample = 30
    else:  # balanced
        default_rm = 0.5
        off_floor = -0.03
        min_sample = 20
    
    # Используем параметры режима в decide_strategy
    strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current,stale,m,h1,h2,
        min_sample=min_sample, off_floor=off_floor)
    
    # Если decide_strategy вернул canary, используем default_rm из режима
    if rm == 0.5:  # canary
        rm = default_rm
```

### 3.3 UI для выбора режима

**Файл:** `web_ui.html` (вкладка "Адаптация")

Добавить dropdown для каждой пары:
```html
<select class="strat-select" onchange="setAdapterMode('{symbol}',this.value)">
    <option value="aggressive">Агрессивный</option>
    <option value="balanced" selected>Сбалансированный</option>
    <option value="tight">Тайтовый</option>
    <option value="manual">Ручной</option>
</select>
```

**Файл:** `server.py` (новый endpoint)
```python
@app.post("/api/adapter/mode/{symbol}")
async def api_adapter_mode(symbol:str, mode:str):
    ov=CONFIG.setdefault("pair_overrides",{}).setdefault(symbol,{})
    ov["adapter_mode"]=mode
    config_api._save()
    logger.info(f"🎛️ {symbol} adapter_mode: {mode}")
    return{"ok":True}
```

---

## 4. UI/визуал

### 4.1 Индикатор "качества входа" (MFE/MAE ratio)

**Файл:** `web_ui.html` (вкладка "История")

Добавить колонку "Захват прибыли":
```html
<div class="h-details">
    <div>MFE: {mfe:.2f}% | MAE: {mae:.2f}% | Захват: {mfe/max(0.01,mfe+abs(mae)):.0%}</div>
</div>
```

**Логика:** `Захват = MFE / (MFE + |MAE|)` — доля прибыли, которую бот "забрал" от пика. Цель: ≥50%.

### 4.2 Часовая тепловая карта

**Файл:** `web_ui.html` (вкладка "Аналитика")

Добавить визуализацию `hour_stats`:
```html
<div class="h-card">
    <div class="h-head"><span class="symbol">🕐 Часовая активность</span></div>
    <div class="h-details">
        {hours.map(h => `<span style="color:${h.net>0?'#3fb950':'#f85149'}">${h.h}:00 (${h.net:+.2f})</span>`).join(' ')}
    </div>
</div>
```

### 4.3 Авто-диагностика (DIAG.md)

**Файл:** `server.py` (новый endpoint `/api/diag`)

```python
@app.get("/api/diag")
async def api_diag():
    diag = []
    for symbol in CONFIG["symbols"]:
        reg = analyzer.regime(state, symbol)
        ov = CONFIG.get("pair_overrides",{}).get(symbol,{})
        lr = adapter.adapter.read_regime(symbol)
        
        # Определяем причину простоя
        reason = "торгуется"
        if ov.get("adapter_strategy") == "OFF":
            reason = "OFF (адаптер)"
        elif ov.get("strategy") == "OFF":
            reason = "OFF (ручной)"
        elif lr[3] < 0.0008:
            reason = f"fee-negative (ATR {lr[3]*100:.3f}% < 0.08%)"
        elif reg["wall_share"] < 0.3 and ov.get("adapter_strategy") == "WALL":
            reason = "нет стен (wall_share < 0.3)"
        
        diag.append({
            "symbol": symbol,
            "strategy": ov.get("adapter_strategy") or ov.get("strategy"),
            "risk_mult": ov.get("risk_mult", 0),
            "atr_pct": lr[3],
            "wall_share": reg["wall_share"],
            "reason": reason
        })
    return {"diag": diag, "timestamp": time.time()}
```

**Скрипт авто-дампа (crontab):**
```bash
*/30 * * * * curl -s http://localhost:8000/api/diag > /root/bybit_scalper/DIAG.md
```

---

## 5. Бонусные фичи (от меня)

### 5.1 Стратегический скоринг (Sharpe ratio)

**Файл:** `analyzer.py:strategy_scores()`

Не только `net_per_trade`, но и `Sharpe = net / std(net)`:

```python
def strategy_scores(rows, min_n=3):
    out={}
    for s,pm in perf_by_strategy(rows).items():
        if pm["n"]==0: continue
        net_list = [r[0] for r in rows if r[8] == s]
        std = statistics.stdev(net_list) if len(net_list) > 1 else 1.0
        sharpe = pm["net"] / std if std > 0 else pm["net"]
        out[s]={"n":pm["n"],"net":pm["net"],"net_per_trade":round(pm["net"]/pm["n"],4),"sharpe":round(sharpe,3)}
    return out
```

**Использование в `decide_strategy()`:**
```python
if prof:
    s,b=max(prof.items(),key=lambda kv:kv[1].get("sharpe",kv[1]["net_per_trade"]))
    return s,(1.0 if b["n"]>=10 else 0.5),f"лучший Sharpe {b['sharpe']:+.2f} (n={b['n']})"
```

### 5.2 Часовая адаптация (авто-блэклист)

**Файл:** `adapter.py:eval()` (после цикла по парам)

Если `hour_stats` показывает токсичные часы (net < -0.5 за 72ч) → добавить в `trading_hours_blacklist`.

```python
def run(self):
    while True:
        await asyncio.sleep(60)
        try:
            # ... существующая логика ...
            
            # Часовая адаптация (раз в 6ч)
            if time.time() - self.last_hour_adapt > 6*3600:
                self.last_hour_adapt = time.time()
                for symbol in self.CONFIG["symbols"]:
                    rows = analyzer.get_rows(self.state.db_conn, symbol, 72)
                    hours = analyzer.hour_stats(rows, worst=3)
                    toxic_hours = [h["h"] for h in hours if h["net"] < -0.5 and h["n"] >= 3]
                    if toxic_hours:
                        ov = self.CONFIG["pair_overrides"][symbol]
                        current_blacklist = ov.get("trading_hours_blacklist", [])
                        new_blacklist = list(set(current_blacklist + toxic_hours))
                        if new_blacklist != current_blacklist:
                            ov["trading_hours_blacklist"] = new_blacklist
                            self.log(symbol, "trading_hours_blacklist", current_blacklist, new_blacklist, f"часовая адаптация: токсичные {toxic_hours}")
                            self.bump(f"часы {symbol}")
                            self.config_api._save()
```

### 5.3 Метрика "качество сделок" (Quality Score)

**Файл:** `analyzer.py` (новая функция)

```python
def quality_score(rows):
    """Оценка качества сделок: комбинация WR, MFE/MAE ratio, commissions/gross."""
    if len(rows) < 5:
        return 0.0
    
    m = trade_metrics(rows)
    wr = m["wr"] / 100.0  # 0..1
    fees_ratio = m["fees"] / m["gross"] if m["gross"] > 0 else 1.0  # 0..1 (меньше = лучше)
    
    # Средний MFE/MAE ratio
    mfe_list = [r[6] for r in rows if r[6] is not None and r[6] > 0]
    mae_list = [abs(r[7]) for r in rows if r[7] is not None and r[7] < 0]
    if mfe_list and mae_list:
        avg_mfe = statistics.mean(mfe_list)
        avg_mae = statistics.mean(mae_list)
        capture_ratio = avg_mfe / (avg_mfe + avg_mae) if (avg_mfe + avg_mae) > 0 else 0.5
    else:
        capture_ratio = 0.5
    
    # Quality Score = WR * (1 - fees_ratio) * capture_ratio
    # Идеал: WR=1.0, fees_ratio=0.0, capture_ratio=1.0 → Score=1.0
    score = wr * (1.0 - fees_ratio) * capture_ratio
    return round(score, 3)
```

**Использование:** Добавить в `/api/analytics` и UI.

---

## 6. Критерии приёмки

- [ ] Все 7 критических багов из секции 1 исправлены
- [ ] Бот торгует автономно без ручных правок (≥1 сделка за 2ч на fee-positive паре)
- [ ] WR ≥ 50% (цель: 55% как в эталоне)
- [ ] Net > 0 за 24ч работы
- [ ] Нет дедлока "все пары OFF" (гарантия разведки работает)
- [ ] UI показывает причину простоя (readiness + DIAG.md)
- [ ] Режимы адаптера работают (aggressive/balanced/tight/manual)
- [ ] Часовая адаптация добавляет токсичные часы в блэклист
- [ ] Sharpe ratio используется для выбора стратегии
- [ ] Quality Score отображается в UI

---

## 7. Порядок работы (отдельные PR)

### PR1: Критические баги (п.1.1–1.7)
- Фикс `regime()` → `atr_pct`
- Фикс `eval()` → `h1,h2`
- Фикс `recommend()` → "OFF" вместо "FEE_NEGATIVE"
- Добавить `compute_required_rm()`, `log_warn_throttle()`
- Удалить двойной `calc_qty`
- Реализовать `risk_change_cooldown`

**Тест:** `pytest test_risk_logic.py` + ручная проверка `/api/pairs` (atr_pct > 0)

### PR2: Автономная логика (п.2.1–2.3)
- Гарантия разведки
- Авто-сброс `allowed_side`
- Авто-ребаланс пар (убыточные → OFF)

**Тест:** Запуск на пустой БД → ≥1 сделка за 2ч

### PR3: Режимы адаптера + UI (п.3.1–3.3, п.4.1–4.3)
- `adapter_mode` (aggressive/balanced/tight/manual)
- UI dropdown
- Индикатор "захват прибыли"
- Часовая тепловая карта
- `/api/diag`

**Тест:** Переключение режимов → проверка `risk_mult` и `off_floor`

### PR4: Бонусные фичи (п.5.1–5.3)
- Sharpe ratio в `strategy_scores()`
- Часовая адаптация
- Quality Score

**Тест:** Проверка `/api/analytics` (sharpe, quality_score)

---

## 8. Проверка

```bash
# После каждого PR:
systemctl restart bybit-scalper.service
journalctl -u bybit-scalper.service -f | grep -E "📋|✅|⚠️|гарантия разведки"

# Проверка API:
curl -s http://185.78.76.248:8000/api/pairs | python3 -c "import sys,json;d=json.load(sys.stdin);[print(f'{s:9} strat={p[\"overrides\"].get(\"adapter_strategy\",\"-\"):6} risk={p[\"overrides\"].get(\"risk_mult\",0):.2f} atr={p[\"regime\"][\"atr_pct\"]*100:.3f}%') for s,p in d.items()]"

# Авто-диагностика:
curl -s http://185.78.76.248:8000/api/diag | python3 -m json.tool
```

---

## 9. Не трогать

- Формат WS-payload `ticker_update` и REST `/api/*` контракты
- Структуру `CONFIG_META` (только расширение)
- Миграции БД — только `ALTER TABLE ... ADD COLUMN`
- Рабочую связку TREND+TP на fee-positive парах (эталон 28.08)

---

## 10. Эталон (28.08, ручная конфигурация)

**HUSDT:** strategy=TREND, risk_mult=1.0, allowed_side=Sell  
**Результат:** 9 сделок, WR 55.6%, Net +$3.32, Gross +$4.93, комиссии $1.61 (33% от gross)  
**TAKE_PROFIT:** 4/4 = +$4.76  
**STOP_LOSS:** 2/2 = -$1.25  

**Вывод:** TREND на паре с ATR ≥ 0.2% + полный размер + TP = рабочая связка. Не ломать.