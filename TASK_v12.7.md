# TASK v12.7: наблюдаемость REST + автостарт + честная канарейка + stale-защита

Ветка: v12.7 (от текущей v12-test с хотфиксом lazy-session). PR в v12.7.
Живой API: http://185.78.76.248:8000
Стек: vanilla JS/CSS, один web_ui.html, без внешних библиотек.

## 0. Контекст и симптомы (06.09.2026)
- analysis_loop жив (last_tick_age 0.18, loop_errors 0), стены тикают (WS ок),
  curl из шелла в Bybit отвечает retCode 0, НО все пары ATR=0.000%, trend=UNKNOWN,
  адаптер пишет "ATR < fee_floor -> OFF" на ВСЕ пары -> 0 сделок.
- Вывод: REST мёртв ВНУТРИ процесса, а fetch_json() глотает любые ошибки молча.
- После рестарта is_trading=False -> бот "Остановлен", пока не нажать СТАРТ.
- Канарейка показывала x0.125 (0.5 режима x 0.25 canary_fraction поверх).
- config_override.json исторически загрязнялся null-значениями (Coerce-спам).
- Эталон не ломать: HUSDT TREND 28.08 — 9 сделок, WR 55.6%, Net +3.32,
  TAKE_PROFIT 4/4 = +4.76, комиссии 33% от gross.

## 1. BUG-1: тихая смерть REST (главный)

### Причина
fetch_json() возвращает None при любой ошибке (нет сессии, non-200, исключение)
без лога и без отражения в health. Пустой bybit_base_url (наследие null-конфига)
даёт относительный URL -> исключение на каждом запросе -> ATR=0 -> каскад OFF.

### Фикс (server.py)
1) Состояние REST + троттлинг-лог:

    _rest_state = {"fails": 0, "last_err": "", "ok": True}

    def rest_fail(msg):
        _rest_state["fails"] += 1
        _rest_state["last_err"] = msg[:80]
        _rest_state["ok"] = False
        log_warn_throttle("REST", msg[:40], f"⚠️ REST down: {msg}", 60)

    async def fetch_json(url, params=None, ep="rest"):
        if not state.http_session:
            await init_http_session()
        if not state.http_session:
            rest_fail("no http session"); return None
        try:
            async with state.http_session.get(url, params=params) as r:
                if r.status != 200:
                    rest_fail(f"HTTP {r.status} {ep}"); return None
                _rest_state["ok"] = True; _rest_state["fails"] = 0
                return await r.json()
        except Exception as e:
            rest_fail(f"{type(e).__name__}: {e} [{ep}]"); return None

2) Валидация base_url на старте (lifespan после init_config_api):

    if not (CONFIG.get("bybit_base_url") or "").startswith("http"):
        logger.warning(f"⚠️ bybit_base_url невалиден ({CONFIG.get('bybit_base_url')!r}) — восстановлен дефолт")
        CONFIG["bybit_base_url"] = "https://api.bybit.com"

3) Health: /api/status -> "health": {..., "rest_ok": _rest_state["ok"],
   "rest_fails": _rest_state["fails"], "rest_last_error": _rest_state["last_err"]}.
4) UI: чип в шапке "REST" зелёный/красный по rest_ok; при rest_ok=false —
   баннер "Нет данных Bybit — решения адаптера заморожены" (см. BUG-5).
5) get_klines/get_symbol_info/get_trend передают ep=("kline"/"info"/"trend")
   для читаемых логов.

## 2. BUG-2: null-загрязнение конфига

### Фикс (config_api.py + server.py)
1) _coerce: в первой строке "if value is None: return None" (без warning).
2) _save: data = {k: cfg.get(k) for k in CONFIG_META if cfg.get(k) is not None}.
3) Миграция на старте (lifespan): прочитать config_override.json, рекурсивно
   удалить ключи со значением None (включая pair_overrides.*), перезаписать файл.
4) Юнит-тест: _coerce(float, None) -> None без исключения; _save не пишет null.

## 3. BUG-3: двойная канарейка x0.125

### Причина
adapter.eval: rm=default_rm(0.5), затем при n<min_sample ещё rm *= canary_fraction(0.25).

### Фикс (adapter.py)
1) Канарейка режима — ЕДИНСТВЕННЫЙ множитель на cold-start:
   balanced=0.5, aggressive=1.0, tight=0.25. canary_fraction применяется ТОЛЬКО
   к пограничному решению decide_strategy (rm=0.25), не поверх режимной.
2) self.canary[symbol] = итоговый rm; reason без дублирующих "canary x0.25".
3) Тест: cold-start balanced даёт x0.5 и ни при какой комбинации не < 0.25,
   кроме явного tight/off.

## 4. BUG-4: is_trading=False после каждого рестарта

### Фикс
1) CONFIG_META += "autostart": {"group":"Система","type":"bool","default":True,
   "desc":"Автозапуск торговли при старте сервиса (paper)."}
2) lifespan: после load_stats — if CONFIG.get("autostart", True):
   state.is_trading=True; logger.info("▶️ автостарт торговли")
3) UI: если is_trading=false — заметная янтарная плашка "Торговля остановлена"
   с кнопкой СТАРТ в один клик (не искать кнопку в шапке).

## 5. BUG-5: OFF на пустых данных (каскад от BUG-1)

### Причина
При ATR=0 (нет данных) recommend() считает пару fee-negative и адаптер
пишет OFF — решение принимается на ОТСУТСТВИИ данных, а не на данных.

### Фикс (adapter.py + analyzer.py)
1) publish_regime уже пишет ts; read_regime возвращать и ts.
2) В eval(): если time.time()-regime_ts > 120 — НЕ принимать решений:
   держать текущие strategy/risk_mult, log_warn_throttle(symbol, "regime_stale",
   "⚠️ regime stale >120с — решения заморожены (REST?)", 120).
3) recommend(): отдельно вернуть ("NO_DATA", ...) если atr_pct==0 и hist пуст;
   decide_strategy() при NO_DATA возвращает текущую стратегию с прежним rm
   (или канарейку 0.5, если текущей нет), НИКОГДА не OFF.
4) Анти-deadlock (разведка) не выбирает кандидатов по atr_pct из live_regime,
   если записи stale — брать максимум atr_pct из state.atr_hist (свежие 30 мин).

## 6. BUG-6: косметика UI (информативность вместо нулей)
1) payload ticker_update += "depth": топ-3 уровня bid/ask [[price,vol],...];
   карточка рисует Bid/Ask-бары из depth (сейчас пустые).
2) Если sinfo пуст (REST info мёртв): чип "размер" = "—" (серый), не ✗.
3) Если atr_pct==0: показывать "ATR: нет данных" янтарным, не "0.000%".
4) Trend UNKNOWN — серый бейдж "—" вместо стрелки.

## 7. Версия
- VERSION="v12.7" (логи, /api/version, заголовок UI из API, service, README)
- после merge: git tag v12.7

## 8. Критерии приёмки
- [ ] Убить REST (фэйковый base_url в тесте) -> в логах "⚠️ REST down" <=60с,
      /api/status rest_ok=false, чип красный, адаптер НЕ пишет OFF (hold)
- [ ] bybit_base_url="" в файле -> на старте восстановлен дефолт + warning
- [ ] autostart=true -> после restart is_trading=true без ручного СТАРТ
- [ ] cold-start balanced = x0.5; x0.125 не встречается нигде
- [ ] regime stale >120с -> решения заморожены, в журнале причина
- [ ] null в config_override.json вычищены миграцией; Coerce-спам = 0
- [ ] UI: depth-бары видны, "размер"='—' без sinfo, "ATR: нет данных" при 0
- [ ] Эталон жив: после восстановления данных HUSDT TREND входит,
      fees/gross <= 50%, WR >= 50% на окне 24ч

## 9. Порядок работы (отдельные PR)
PR1: BUG-1 + BUG-4 (REST-наблюдаемость, валидация base_url, autostart)
PR2: BUG-2 + BUG-3 (null-миграция, coerce-гард, одна канарейка) + тесты
PR3: BUG-5 (stale-hold, NO_DATA в recommend/decide)
PR4: BUG-6 (UI) + версия v12.7
После каждого PR: деплой, restart, проверка журнала и /api/status.

## 10. Проверка
    systemctl restart bybit-scalper.service
    journalctl -u bybit-scalper.service -n 5 --no-pager        # v12.7, автостарт
    curl -s localhost:8000/api/status | python3 -m json.tool   # rest_ok, is_trading
    curl -s localhost:8000/api/pairs | python3 -c "
    import sys,json
    for s,p in json.load(sys.stdin).items():
        r=p.get('regime',{})
        print(f\"{s:9} ATR={(r.get('atr_pct') or 0)*100:.3f}% fee_pos={p.get('fee_positive')} active={p.get('active')}\")"
    sqlite3 knowledge.db "SELECT COUNT(*) FROM adaptive_log WHERE ts > strftime('%s','now')-120"

## 11. Не трогать
- WS-payload ticker_update (только добавлять поля), REST-контракты
- Логику входов/выходов стратегий и TP/SL (эталон 28.08 работает)
- Миграции: только очистка null + ALTER ADD