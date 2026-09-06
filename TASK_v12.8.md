# TASK v12.8: Автоподбор торговых пар из Bybit API в live-режиме

Ветка: v12.8 (PR в неё).
Живой API: http://185.78.76.248:8000
Контекст: сейчас пары хардкодятся в CONFIG["symbols"] (BTC/ETH/SOL/HUSDT/GRAM).
Проблема: в спокойном рынке все fee-negative → простой; в волатильном — упускаем
новые листинги. Цель: бот сам сканирует Bybit, выбирает fee-positive пары с
высокой ликвидностью, добавляет/убирает без рестарта.

## 0. Принципы

1. **Автономность:** бот сам выбирает лучшие пары по ATR/ликвидности/спреду.
2. **Защита:** чёрный список (fee-negative мажоры), белый список (эталонные пары).
3. **Безопасность:** не добавлять пары с низкой ликвидностью (риск проскальзывания).
4. **Наблюдаемость:** UI показывает кандидатов, причины выбора/исключения.
5. **Rate-limit:** не долбить Bybit API чаще раза в час.

---

## 1. Требования

### 1.1 Сканирование Bybit API

**Endpoint:** `GET /v5/market/instruments-info?category=linear`
**Возвращает:** список всех Linear USDT-пар с метаданными:
```json
{
  "result": {
    "list": [
      {
        "symbol": "BTCUSDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "status": "Trading",
        "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"},
        "priceFilter": {"tickSize": "0.1"},
        "launchTime": "1670601600000"
      },
      ...
    ]
  }
}
```

**Фильтры:**
- `status == "Trading"` (активные пары)
- `quoteCoin == "USDT"` (только USDT-пары)
- Ликвидность: 24ч объём ≥ $1M (из `/v5/market/tickers`)
- Спред: ≤ 0.1% (из orderbook snapshot)

### 1.2 Скоринг пар

Для каждой пары считать:
```python
score = (
    0.4 * normalized_atr_pct +      # волатильность (главный фактор)
    0.3 * normalized_volume +       # ликвидность
    0.2 * normalized_spread_inv +   # узкий спред (инвертированный)
    0.1 * normalized_age_days       # возраст (старые пары стабильнее)
)
```

**Нормализация:** min-max scaling по всем парам (0..1).

**Приоритет:** пары с score ≥ 0.7 → кандидаты на добавление.

### 1.3 Чёрный/белый списки

**Чёрный список (всегда OFF):**
- BTCUSDT, ETHUSDT — fee-negative (ATR < 0.16%), исторически убыточные
- Пары с объёмом < $500K за 24ч
- Пары младше 7 дней (нестабильные)

**Белый список (всегда ON):**
- HUSDT, AKEUSDT, STXUSDT — эталонные пары (исторически прибыльные)
- Можно расширять через UI

### 1.4 Автодобавление/удаление

**Добавление:** если пара в топ-10 по score и не в CONFIG["symbols"] → добавить через `apply_symbols()`.

**Удаление:** если пара в CONFIG["symbols"], но:
- ATR < fee-floor за последние 24ч (3 из 4 замеров)
- Объём упал ниже $500K
- Спред > 0.1% за последние 6ч

→ убрать через `apply_symbols()`.

**Лимиты:**
- Максимум 10 активных пар (защита от распыления)
- Минимум 3 пары (эталон + 2 кандидата)

### 1.5 Периодичность

- **Полное сканирование:** раз в 6 часов (rate-limit Bybit)
- **Быстрая проверка:** раз в час (только текущие пары, без добавления новых)
- **Ручной триггер:** кнопка "Сканировать сейчас" в UI

### 1.6 Сохранение

- Выбранные пары пишутся в `config_override.json` → `symbols`
- История сканирований в `knowledge.db` (таблица `pair_scans`)
- Лог добавлений/удалений в `adaptive_log`

---

## 2. Архитектура

### 2.1 Новый модуль: `pair_selector.py`

```python
# pair_selector.py - автоподбор пар из Bybit API
import time, aiohttp, statistics
from typing import List, Dict, Tuple

class PairSelector:
    def __init__(self, state, config, config_api):
        self.state = state
        self.config = config
        self.config_api = config_api
        self.last_scan = 0
        self.scan_interval = 6 * 3600  # 6 часов
        self.blacklist = {"BTCUSDT", "ETHUSDT"}
        self.whitelist = {"HUSDT", "AKEUSDT", "STXUSDT"}
        self.max_pairs = 10
        self.min_pairs = 3
    
    async def scan_bybit(self) -> List[Dict]:
        """Сканирует все Linear USDT-пары на Bybit."""
        if not self.state.http_session:
            from server import init_http_session
            await init_http_session()
        
        # Получаем список инструментов
        url = f"{self.config['bybit_base_url']}/v5/market/instruments-info"
        params = {"category": "linear", "limit": "1000"}
        async with self.state.http_session.get(url, params=params) as r:
            if r.status != 200:
                return []
            data = await r.json()
        
        instruments = data.get("result", {}).get("list", [])
        
        # Фильтруем: status=Trading, quoteCoin=USDT
        candidates = []
        for inst in instruments:
            if inst.get("status") != "Trading":
                continue
            if inst.get("quoteCoin") != "USDT":
                continue
            symbol = inst["symbol"]
            if symbol in self.blacklist:
                continue
            
            candidates.append({
                "symbol": symbol,
                "min_qty": float(inst.get("lotSizeFilter", {}).get("minOrderQty", 0)),
                "qty_step": float(inst.get("lotSizeFilter", {}).get("qtyStep", 0)),
                "tick_size": float(inst.get("priceFilter", {}).get("tickSize", 0)),
                "launch_time": int(inst.get("launchTime", 0)) / 1000
            })
        
        # Получаем тикеры (объём, цена)
        tickers_url = f"{self.config['bybit_base_url']}/v5/market/tickers"
        tickers_params = {"category": "linear"}
        async with self.state.http_session.get(tickers_url, params=tickers_params) as r:
            if r.status != 200:
                return []
            tickers_data = await r.json()
        
        tickers = {t["symbol"]: t for t in tickers_data.get("result", {}).get("list", [])}
        
        # Обогащаем кандидатов данными из тикеров
        for cand in candidates:
            sym = cand["symbol"]
            if sym not in tickers:
                continue
            t = tickers[sym]
            cand["last_price"] = float(t.get("lastPrice", 0))
            cand["volume_24h"] = float(t.get("volume24h", 0))
            cand["turnover_24h"] = float(t.get("turnover24h", 0))
            cand["bid_price"] = float(t.get("bid1Price", 0))
            cand["ask_price"] = float(t.get("ask1Price", 0))
        
        return candidates
    
    async def compute_atr(self, symbol: str) -> float:
        """Считает ATR% за последние 24ч (144 свечи 10мин)."""
        from server import get_klines
        kl = await get_klines(symbol, "10", 144)
        if not kl or len(kl) < 14:
            return 0.0
        
        # calc_atr из server.py
        tr = []
        for i in range(1, len(kl)):
            h, l, pc = float(kl[i][2]), float(kl[i][3]), float(kl[i-1][4])
            tr.append(max(h-l, abs(h-pc), abs(l-pc)))
        atr = sum(tr[-14:]) / 14
        last_price = float(kl[-1][4])
        return (atr / last_price) if last_price > 0 else 0.0
    
    async def score_pairs(self, candidates: List[Dict]) -> List[Dict]:
        """Считает скоринг для каждой пары."""
        # Нормализация: собираем все значения
        atrs = []
        volumes = []
        spreads = []
        ages = []
        
        now = time.time()
        for cand in candidates:
            atr = await self.compute_atr(cand["symbol"])
            cand["atr_pct"] = atr
            atrs.append(atr)
            
            vol = cand.get("turnover_24h", 0)
            cand["volume_24h_usd"] = vol
            volumes.append(vol)
            
            bid = cand.get("bid_price", 0)
            ask = cand.get("ask_price", 0)
            spread = (ask - bid) / bid if bid > 0 else 1.0
            cand["spread_pct"] = spread
            spreads.append(spread)
            
            age_days = (now - cand.get("launch_time", now)) / 86400
            cand["age_days"] = age_days
            ages.append(age_days)
        
        # Min-max нормализация
        def normalize(values):
            if not values:
                return []
            min_v, max_v = min(values), max(values)
            if max_v == min_v:
                return [0.5] * len(values)
            return [(v - min_v) / (max_v - min_v) for v in values]
        
        norm_atrs = normalize(atrs)
        norm_vols = normalize(volumes)
        norm_spreads_inv = normalize([-s for s in spreads])  # инвертируем (меньше = лучше)
        norm_ages = normalize(ages)
        
        # Скоринг
        for i, cand in enumerate(candidates):
            score = (
                0.4 * norm_atrs[i] +
                0.3 * norm_vols[i] +
                0.2 * norm_spreads_inv[i] +
                0.1 * norm_ages[i]
            )
            cand["score"] = score
        
        # Сортировка по score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates
    
    async def select_pairs(self) -> Tuple[List[str], List[Dict]]:
        """Выбирает пары для торговли."""
        candidates = await self.scan_bybit()
        if not candidates:
            return list(self.config["symbols"]), []
        
        # Фильтры
        filtered = []
        for cand in candidates:
            # Чёрный список
            if cand["symbol"] in self.blacklist:
                continue
            # Ликвидность ≥ $500K
            if cand.get("volume_24h_usd", 0) < 500000:
                continue
            # Возраст ≥ 7 дней
            if cand.get("age_days", 0) < 7:
                continue
            # ATR ≥ fee-floor
            if cand.get("atr_pct", 0) < self.config.get("min_atr_pct_abs", 0.0016):
                continue
            # Спред ≤ 0.1%
            if cand.get("spread_pct", 1.0) > 0.001:
                continue
            filtered.append(cand)
        
        # Скоринг
        scored = await self.score_pairs(filtered)
        
        # Белый список всегда в
        selected = list(self.whitelist)
        
        # Добавляем топ-пары до лимита
        for cand in scored:
            if len(selected) >= self.max_pairs:
                break
            if cand["symbol"] not in selected:
                selected.append(cand["symbol"])
        
        # Минимум min_pairs
        if len(selected) < self.min_pairs:
            for cand in scored:
                if cand["symbol"] not in selected:
                    selected.append(cand["symbol"])
                if len(selected) >= self.min_pairs:
                    break
        
        return selected, scored[:20]  # топ-20 кандидатов для UI
    
    async def run_scan(self, force: bool = False):
        """Запускает сканирование и обновляет CONFIG["symbols"]."""
        now = time.time()
        if not force and (now - self.last_scan) < self.scan_interval:
            return
        
        self.last_scan = now
        selected, candidates = await self.select_pairs()
        
        old_symbols = list(self.config["symbols"])
        new_symbols = selected
        
        added = set(new_symbols) - set(old_symbols)
        removed = set(old_symbols) - set(new_symbols)
        
        if added or removed:
            self.config["symbols"] = new_symbols
            self.config_api._save()
            
            # Логируем изменения
            from adapter import adapter
            for sym in added:
                adapter.log("SYSTEM", "symbols", old_symbols, new_symbols,
                           f"автодобавление: {sym}")
            for sym in removed:
                adapter.log("SYSTEM", "symbols", old_symbols, new_symbols,
                           f"автоудаление: {sym}")
            
            # Применяем через apply_symbols
            from server import apply_symbols
            await apply_symbols(new_symbols)
        
        # Сохраняем историю сканирований
        self.save_scan_history(selected, candidates)
    
    def save_scan_history(self, selected: List[str], candidates: List[Dict]):
        """Сохраняет историю сканирований в knowledge.db."""
        from adapter import adapter
        conn = adapter.kn_conn
        cur = adapter.kn_cur
        
        # Создаём таблицу если нет
        cur.execute("""CREATE TABLE IF NOT EXISTS pair_scans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            selected TEXT,
            candidates TEXT
        )""")
        
        cur.execute("INSERT INTO pair_scans(ts, selected, candidates) VALUES(?,?,?)",
                   (time.time(), json.dumps(selected), json.dumps(candidates[:20])))
        conn.commit()

pair_selector = PairSelector(None, None, None)
```

### 2.2 Интеграция в server.py

```python
# В lifespan:
pair_selector.state = state
pair_selector.config = CONFIG
pair_selector.config_api = config_api
asyncio.create_task(pair_selector_loop())

async def pair_selector_loop():
    while True:
        try:
            await pair_selector.run_scan()
            await asyncio.sleep(3600)  # раз в час быстрая проверка
        except Exception as e:
            logger.error(f"pair_selector_loop error: {e}")

# Новый endpoint для ручного триггера:
@app.post("/api/scan-pairs")
async def api_scan_pairs():
    await pair_selector.run_scan(force=True)
    return {"ok": True, "symbols": CONFIG["symbols"]}

# Новый endpoint для UI (кандидаты):
@app.get("/api/pair-candidates")
async def api_pair_candidates():
    _, candidates = await pair_selector.select_pairs()
    return {"candidates": candidates}
```

### 2.3 UI: вкладка "Пары"

```html
<div class="pairs-tab">
  <h2>🔍 Автоподбор пар</h2>
  
  <div class="controls">
    <button onclick="scanPairs()">Сканировать сейчас</button>
    <span id="last-scan">Последнее сканирование: ...</span>
  </div>
  
  <h3>Активные пары ({{selected.length}})</h3>
  <div class="pair-grid">
    <div v-for="sym in selected" class="pair-card active">
      <span class="symbol">{{sym}}</span>
      <button onclick="removePair('{{sym}}')">✗</button>
    </div>
  </div>
  
  <h3>Кандидаты (топ-20)</h3>
  <div class="pair-grid">
    <div v-for="cand in candidates" class="pair-card candidate">
      <span class="symbol">{{cand.symbol}}</span>
      <span class="score">Score: {{cand.score.toFixed(2)}}</span>
      <span class="atr">ATR: {{(cand.atr_pct*100).toFixed(3)}}%</span>
      <span class="volume">Vol: ${{(cand.volume_24h_usd/1e6).toFixed(1)}}M</span>
      <button onclick="addPair('{{cand.symbol}}')">+</button>
    </div>
  </div>
  
  <h3>Чёрный список</h3>
  <div class="pair-grid">
    <div v-for="sym in blacklist" class="pair-card blacklisted">
      <span class="symbol">{{sym}}</span>
      <span class="reason">fee-negative</span>
    </div>
  </div>
</div>
```

---

## 3. Критерии приёмки

- [ ] Бот сканирует Bybit API раз в 6ч (без rate-limit ошибок)
- [ ] Выбирает топ-10 пар по score (ATR + ликвидность + спред)
- [ ] Белый список (HUSDT/AKE/STX) всегда в, чёрный (BTC/ETH) всегда OUT
- [ ] Автодобавление/удаление через `apply_symbols()` без рестарта
- [ ] UI показывает кандидатов с score/ATR/volume
- [ ] Кнопка "Сканировать сейчас" работает
- [ ] История сканирований в `knowledge.db` (таблица `pair_scans`)
- [ ] Лимиты: максимум 10 пар, минимум 3 пары
- [ ] Эталон не сломан: HUSDT TREND работает как раньше

---

## 4. Порядок работы (отдельные PR)

### PR1: pair_selector.py + интеграция
- Новый модуль `pair_selector.py`
- Интеграция в `lifespan` (server.py)
- Endpoints `/api/scan-pairs`, `/api/pair-candidates`
- Таблица `pair_scans` в knowledge.db

### PR2: UI вкладка "Пары"
- Новая вкладка в web_ui.html
- Кнопка "Сканировать сейчас"
- Сетка активных пар и кандидатов
- Чёрный/белый списки

### PR3: Тесты
- `test_pair_selector.py`: скоринг, фильтры, лимиты
- Интеграционный тест: mock Bybit API, проверка `apply_symbols()`

---

## 5. Проверка

```bash
# Ручной триггер
curl -s -X POST localhost:8000/api/scan-pairs | python3 -m json.tool

# Проверка выбранных пар
curl -s localhost:8000/api/status | python3 -c "import sys,json;print('symbols:',json.load(sys.stdin)['symbols'])"

# Кандидаты
curl -s localhost:8000/api/pair-candidates | python3 -c "
import sys,json
cands=json.load(sys.stdin)['candidates']
for c in cands[:10]:
    print(f\"{c['symbol']:12} score={c['score']:.2f} ATR={c['atr_pct']*100:.3f}% vol=${c['volume_24h_usd']/1e6:.1f}M\")"

# История сканирований
sqlite3 knowledge.db "SELECT ts, selected FROM pair_scans ORDER BY ts DESC LIMIT 5"
```

---

## 6. Бонусы

### 6.1 Адаптивный скоринг
Если пара в белом списке, но score низкий → логировать warning ("эталонная пара деградирует").

### 6.2 Сезонность
Анализировать, какие пары лучше в какие часы (из `hour_stats`), добавлять/убирать по расписанию.

### 6.3 Корреляция
Не добавлять пары с корреляцией > 0.8 к уже активным (диверсификация).

---

## 7. Не трогать

- Логику входов/выходов (TREND/WALL/SWING работают правильно)
- Адаптер и анализатор (v12.7 стабильна)
- WS-payload ticker_update и REST-контракты (только добавлять endpoints)
- Миграции: только CREATE TABLE `pair_scans`

---

## 8. Эталон (не сломать)

**HUSDT TREND (28.08):** 9 сделок, WR 55.6%, Net +$3.32, TAKE_PROFIT 4/4, комиссии 33% от gross.
Эта пара должна оставаться в белом списке и работать как раньше.