# TASK v13: адаптер, который учится на своих сделках

Ветка: v13-adapter (PR в неё). Живой API: http://185.78.76.248:8000
Файлы: adapter.py, analyzer.py (server.py не трогаем, кроме передачи perf).

## 0. Принцип
Адаптер должен замечать СВОИ патологии и реагировать за 1-2 eval,
а не ждать 20 сделок и условий, которых в наших данных никогда нет.

## 1. Данные (почему это нужно)

### Эталон (28.08, ручная настройка) — ЦЕЛЬ
9 сделок HUSDT TREND Sell/BEARISH: WR 55.6%, Net +$3.32, TP 4/4 = +$4.76,
комиссии 33% от gross, ср. длительность 134с.

### v12.7 (06.09, автономно) — ПРОВАЛ, который адаптер не заметил
32 сделки: WR 12.5%, Net -$13.47, Gross -$6.99 (убыток ДО комиссий).
- STOP_LOSS: 0/18 = -$17.17, ср. 238с, MFE=0.00 у всех (входы «мёртвые»)
- TAKE_PROFIT: 4/4 = +$6.09 (работает — НЕ ЛОМАТЬ)
- TRAILING_STOP: 0/8, gross +$1.40, net -$0.42 (комиссии съедают)
- Buy: 0/3; HUSDT: 0/11; часы 10-13: 31 сделка, -$13.26
- ±0 сделок (gross>0, net<=0): 8 из 32

## 2. Диагноз: текущие adaptive_rules слепы

| Правило | Триггер | Наши данные | Вердикт |
|---|---|---|---|
| 1 | inst_stop(<30с) > 35% в обеих половинах | inst_stop ≈ 0.11 (стопы 238с) | мертво |
| 2 | avg_mfe > 0.008 | avg_mfe ≈ 0.005 | мертво |
| 3 | avg_mae > -0.002 и inst>0.3 | avg_mae ≈ -0.01 | мертво |
| гейт | n >= min_sample(20) | HUSDT n=11 | блокирует |
| охват | wall_min_age/min_sl_distance/trail_atr | сливает TREND, а trend_sl_atr_mult не крутится | не те параметры |

Вывод: адаптер не видит «SL 0 из N», «MFE=0 на убытках», «gross>0 net<=0»,
«Buy 0/3», «токсичные часы» — и не адаптирует ВХОДЫ вообще.

## 3. Требуемые изменения

### 3.1 Передавать perf_by_strategy в adaptive_rules
adapter.eval():
    changes += analyzer.adaptive_rules(m, h1, h2, self.get_param, symbol,
                                       analyzer.perf_by_strategy(rows))

### 3.2 Новые правила выходов (analyzer.adaptive_rules, добавить к старым)

    def adaptive_rules(m, h1, h2, get, symbol, perf=None):
        out = []
        n = m.get("n", 0)
        if n < 10: return out          # гейт ниже для выходов (было 20)
        perf = perf or {}
        sl = perf.get("STOP_LOSS"); tr = perf.get("TRAILING_STOP")
        ts = perf.get("TIME_STOP")
        # 1) SL никогда не win при >=5 попытках -> расширить стоп TREND
        if sl and sl["n"] >= 5 and sl["wr"] == 0:
            out.append(("trend_sl_atr_mult",
                        min(4.0, get(symbol, "trend_sl_atr_mult") * 1.25),
                        f"SL 0/{sl['n']} побед — расширить стоп TREND"))
        # 2) MFE≈0 на убытках + WR<30 -> входы мёртвые: ужесточить подтверждение
        if m.get("avg_mfe", 0) < 0.003 and m.get("wr", 0) < 30:
            out.append(("imbalance_confirmation_ticks",
                        min(10, get(symbol, "imbalance_confirmation_ticks") + 2),
                        "MFE≈0 на убытках — ужесточить подтверждение входа"))
        # 3) gross>0, net<=0 -> комиссии съедают: поднять порог трейлинга
        if m.get("gross", 0) > 0 and m.get("net", 0) <= 0:
            out.append(("trail_activation_pct",
                        min(0.02, get(symbol, "trail_activation_pct") * 1.3),
                        "gross>0 net<=0 — трейлинг отдаёт комиссии"))
        # 4) trailing gross>0 net<=0 -> дать прибыли дышать
        if tr and tr["n"] >= 3 and tr["gross"] > 0 and tr["net"] <= 0:
            out.append(("trend_trail_atr_mult",
                        min(6.0, get(symbol, "trend_trail_atr_mult") * 1.2),
                        "trailing gross>0 net<=0 — дать прибыли дышать"))
        # 5) TIME_STOP в минус -> сократить время мёртвых сделок
        if ts and ts["n"] >= 2 and ts["net"] < 0:
            out.append(("time_stop_seconds",
                        max(300, int(get(symbol, "time_stop_seconds") * 0.7)),
                        "TIME_STOP в минус — сократить тайм-стоп"))
        return out[:2]

### 3.3 Адаптация ВХОДОВ (детектор мёртвых входов)
analyzer: добавить метрику в trade_metrics:
    dead = [r for r in losses if (r[6] or 0) < 0.002]   # MFE<0.2% на убытке
    "dead_entry_rate": round(len(dead)/len(losses),2) if losses else 0.0
adapter: если dead_entry_rate > 0.6 и n >= 10:
    mtf_min_confirms +1 (cap 2), require_trend_alignment=True,
    loss_cooldown_seconds = max(текущий, 300). Лог с метриками.

### 3.4 Сторона по перформансу
adapter: perf по сторонам (Buy/Sell gross из trade_metrics):
    если Buy n>=3 и buy_gross<=0 и sell_gross>0 -> allowed_side="Sell"
    (cooldown 6ч, лог). Автосброс в BOTH при buy_gross>0 за следующие 10 сделок
    или при смене 1h-тренда (уже есть) — не дублировать, только перформанс-триггер.

### 3.5 Быстрый ребаланс пары (6ч, вместо 24ч)
Если за 24ч: net < -5 и wr < 20% и n >= 8 -> risk_mult *= 0.5 (floor 0.25),
лог «пара деградирует»; восстановление: 3 прибыльные из последних 5 -> вернуть rm.
Старый 24ч-ребаланс 8/10 -> OFF оставить как крайнюю меру.

### 3.6 Часы: быстрее и строже
Часовую адаптацию запускать раз в 1ч (было 6ч); порог: net < -0.5 и n >= 3
(оставить), но добавлять час в blacklist пары сразу, лог; снимать час, если за
следующие 24ч он дал net > 0 (иначе блэклист только растёт).

### 3.7 Кулдауны и анти-осцилляция
adapter.__init__: self.last_rule_ts = {}
Применение правил: не чаще 1 раза в 6ч на (symbol, param):
    for param, old, new, rsn in rules:
        key = (symbol, param)
        if time.time() - self.last_rule_ts.get(key, 0) < 6*3600: continue
        changes.append((param, old, new, rsn)); self.last_rule_ts[key] = time.time()
Каждое изменение параметра — в adaptive_log со снапшотом метрик
(wr, n, avg_mfe, sl_n, sl_wr, dead_entry_rate).

### 3.8 Наблюдаемость
/api/adapter: добавить поле "rules_applied_24h" (count по параметрам) и
"last_metrics" (m + perf) — чтобы в UI было видно, НА ЧТО адаптер смотрит.

## 4. Не ломать (работает по эталону)
- TAKE_PROFIT логика (4/4 в обоих наборах) — параметры TP не адаптировать вниз
- Одна канарейка режима (×0.5 balanced), canary_fraction только для пограничных 0.25
- Cold-start исключения (первая смена OFF->стратегия без стрика/кулдауна)
- anti-deadlock разведка и stale-hold (v12.7)

## 5. Критерии приёмки (24ч прогона)
- [ ] Сделок <= 12, WR >= 40%, Net >= 0
- [ ] STOP_LOSS <= 6 за сутки; при 0/5 адаптер САМ расширил trend_sl_atr_mult (лог есть)
- [ ] dead_entry_rate > 0.6 -> ужесточение входов (лог есть)
- [ ] Buy при 0/3 -> allowed_side=Sell (лог есть)
- [ ] Каждый параметр меняется <= 2 раз за 24ч (нет осцилляций)
- [ ] TP остаётся 100% winrate на развитых позициях
- [ ] /api/adapter показывает last_metrics и rules_applied_24h

## 6. Порядок работы (PR)
PR1: perf в adaptive_rules + правила 3.2 + кулдауны 3.7 + тесты на синтетике
     (синтетические rows: SL 0/6 -> ждём рост trend_sl_atr_mult; MFE=0 -> тики входа)
PR2: адаптация входов 3.3 + сторона 3.4
PR3: ребаланс пары 3.5 + часы 3.6 + наблюдаемость 3.8

## 7. Проверка
    systemctl restart bybit-scalper.service
    sleep 120
    curl -s localhost:8000/api/adapter | python3 -m json.tool | head -60
    sqlite3 knowledge.db "SELECT symbol,param,old,new,reason FROM adaptive_log ORDER BY id DESC LIMIT 10"

## 8. Не трогать
- server.py кроме передачи perf; entry_*/manage_position; WS/REST контракты
- CONFIG_META только расширять; миграции только ALTER ADD

## 9. Мост оператора (до merge PR1)
Пока адаптер слепой, дефолты выставлены вручную и должны стать новыми
базовыми значениями после PR1:
    trend_sl_atr_mult 2.5, min_sl_distance_pct 0.004,
    imbalance_confirmation_ticks 5, mtf_min_confirms 2, loss_cooldown_seconds 300