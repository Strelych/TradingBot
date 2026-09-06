# test_adaptive_rules.py - юнит-тесты на синтетических данных для TASK v13 PR1
# Проверка новых правил adaptive_rules:
# 1) SL 0/6 -> рост trend_sl_atr_mult
# 2) MFE=0 -> ужесточение входов (imbalance_confirmation_ticks)

import sys
sys.path.insert(0, '/workspace')

import analyzer

def make_row(pnl, gross, reason, side, exit_ts, ts, mfe, mae, strategy):
    """Создаёт синтетическую строку сделки."""
    return (pnl, gross, reason, side, exit_ts, ts, mfe, mae, strategy)

def get_param_mock(symbol, param):
    """Mock для get_param с дефолтными значениями."""
    defaults = {
        "trend_sl_atr_mult": 2.0,
        "imbalance_confirmation_ticks": 5,
        "trail_activation_pct": 0.01,
        "trend_trail_atr_mult": 4.0,
        "time_stop_seconds": 600,
        "min_sample": 10,
    }
    return defaults.get(param, 0)

def test_sl_0_from_6():
    """Тест: SL 0 из 6 побед -> должен вырасти trend_sl_atr_mult."""
    print("Тест 1: SL 0/6 -> рост trend_sl_atr_mult")
    
    # Синтетические rows: 6 сделок STOP_LOSS с pnl < 0, wr=0
    rows = []
    base_ts = 1700000000
    for i in range(6):
        # STOP_LOSS сделка: pnl=-1.0, gross=-1.0, mfe=0.001 (мало), mae=-0.01
        rows.append(make_row(
            pnl=-1.0,
            gross=-1.0,
            reason="STOP_LOSS",
            side="Sell",
            exit_ts=base_ts + i*300 + 200,
            ts=base_ts + i*300,
            mfe=0.001,
            mae=-0.01,
            strategy="TREND"
        ))
    
    # Добавим ещё 4 сделки TAKE_PROFIT для достижения n>=10
    for i in range(4):
        rows.append(make_row(
            pnl=1.0,
            gross=1.2,
            reason="TAKE_PROFIT",
            side="Sell",
            exit_ts=base_ts + 2000 + i*300,
            ts=base_ts + 2000 + i*300 - 100,
            mfe=0.015,
            mae=-0.002,
            strategy="TREND"
        ))
    
    m = analyzer.trade_metrics(rows)
    h1, h2 = analyzer.halves(rows)
    perf = analyzer.perf_by_strategy(rows)
    
    print(f"  metrics: n={m['n']}, wr={m['wr']}, avg_mfe={m['avg_mfe']}")
    print(f"  perf STOP_LOSS: {perf.get('STOP_LOSS')}")
    
    rules = analyzer.adaptive_rules(m, h1, h2, get_param_mock, "TEST", perf)
    
    # Ожидаем правило для trend_sl_atr_mult
    sl_rules = [r for r in rules if r[0] == "trend_sl_atr_mult"]
    assert len(sl_rules) > 0, "Ожидалось правило trend_sl_atr_mult"
    
    rule = sl_rules[0]
    param, new, reason = rule  # 3 элемента: param, new_value, reason
    old = get_param_mock("TEST", param)  # получаем старое значение из мока
    print(f"  Правило: {param}: {old} -> {new}")
    print(f"  Причина: {reason}")
    
    assert "SL 0/" in reason, f"Причина должна содержать 'SL 0/': {reason}"
    assert new > old, f"trend_sl_atr_mult должен вырасти: {old} -> {new}"
    assert new <= 4.0, f"trend_sl_atr_mult не должен превышать 4.0: {new}"
    
    print("  ✓ Тест пройден\n")
    return True

def test_mfe_zero_strict_entry():
    """Тест: MFE≈0 на убытках + WR<30 -> ужесточение входов."""
    print("Тест 2: MFE≈0 на убытках -> ужесточение входов")
    
    # Синтетические rows: низкий MFE, низкий WR
    rows = []
    base_ts = 1700000000
    
    # 8 убыточных сделок с MFE≈0 (но не STOP_LOSS чтобы не триггерить правило 1)
    for i in range(8):
        rows.append(make_row(
            pnl=-0.5,
            gross=-0.5,
            reason="TIME_STOP",  # Не STOP_LOSS чтобы избежать правила SL
            side="Buy",
            exit_ts=base_ts + i*300 + 150,
            ts=base_ts + i*300,
            mfe=0.0005,  # MFE ≈ 0 (должно быть < 0.003 после усреднения)
            mae=-0.008,
            strategy="TREND"
        ))
    
    # 2 прибыльных для n=10 с низким MFE чтобы avg_mfe остался < 0.003
    for i in range(2):
        rows.append(make_row(
            pnl=0.8,
            gross=1.0,
            reason="TAKE_PROFIT",
            side="Buy",
            exit_ts=base_ts + 3000 + i*300,
            ts=base_ts + 3000 + i*300 - 100,
            mfe=0.001,  # Низкий MFE
            mae=-0.001,
            strategy="TREND"
        ))
    
    m = analyzer.trade_metrics(rows)
    h1, h2 = analyzer.halves(rows)
    perf = analyzer.perf_by_strategy(rows)
    
    print(f"  metrics: n={m['n']}, wr={m['wr']}, avg_mfe={m['avg_mfe']}")
    
    rules = analyzer.adaptive_rules(m, h1, h2, get_param_mock, "TEST", perf)
    
    # Ожидаем правило для imbalance_confirmation_ticks
    entry_rules = [r for r in rules if r[0] == "imbalance_confirmation_ticks"]
    assert len(entry_rules) > 0, f"Ожидалось правило imbalance_confirmation_ticks, получено: {rules}"
    
    rule = entry_rules[0]
    param, new, reason = rule
    old = get_param_mock("TEST", param)
    print(f"  Правило: {param}: {old} -> {new}")
    print(f"  Причина: {reason}")
    
    assert "MFE" in reason or "ужесточить" in reason.lower(), f"Причина должна упоминать MFE или ужесточение: {reason}"
    assert new > old, f"imbalance_confirmation_ticks должен вырасти: {old} -> {new}"
    assert new <= 10, f"imbalance_confirmation_ticks не должен превышать 10: {new}"
    
    print("  ✓ Тест пройден\n")
    return True

def test_gross_positive_net_negative():
    """Тест: gross>0, net<=0 -> комиссии съедают, поднять trail_activation_pct."""
    print("Тест 3: gross>0 net<=0 -> поднять порог трейлинга")
    
    rows = []
    base_ts = 1700000000
    
    # Сделки где gross>0 но net<=0 (комиссии съедают)
    for i in range(10):
        # gross положительный, но net отрицательный из-за комиссий
        gross = 0.5
        pnl = -0.1  # net после комиссий
        rows.append(make_row(
            pnl=pnl,
            gross=gross,
            reason="TRAILING_STOP",
            side="Sell",
            exit_ts=base_ts + i*300 + 200,
            ts=base_ts + i*300,
            mfe=0.008,
            mae=-0.003,
            strategy="TREND"
        ))
    
    m = analyzer.trade_metrics(rows)
    h1, h2 = analyzer.halves(rows)
    perf = analyzer.perf_by_strategy(rows)
    
    print(f"  metrics: n={m['n']}, gross={m['gross']}, net={m['net']}")
    
    rules = analyzer.adaptive_rules(m, h1, h2, get_param_mock, "TEST", perf)
    
    # Ожидаем правило для trail_activation_pct
    trail_rules = [r for r in rules if r[0] == "trail_activation_pct"]
    assert len(trail_rules) > 0, f"Ожидалось правило trail_activation_pct, получено: {rules}"
    
    rule = trail_rules[0]
    param, new, reason = rule
    old = get_param_mock("TEST", param)
    print(f"  Правило: {param}: {old} -> {new}")
    print(f"  Причина: {reason}")
    
    assert "gross>0 net<=0" in reason or "комиссии" in reason.lower(), f"Причина должна упоминать комиссии: {reason}"
    assert new > old, f"trail_activation_pct должен вырасти: {old} -> {new}"
    assert new <= 0.02, f"trail_activation_pct не должен превышать 0.02: {new}"
    
    print("  ✓ Тест пройден\n")
    return True

def test_no_rules_when_n_less_than_10():
    """Тест: при n<10 правила не применяются."""
    print("Тест 4: n<10 -> нет правил")
    
    rows = []
    base_ts = 1700000000
    
    # Только 5 сделок
    for i in range(5):
        rows.append(make_row(
            pnl=-1.0,
            gross=-1.0,
            reason="STOP_LOSS",
            side="Sell",
            exit_ts=base_ts + i*300 + 200,
            ts=base_ts + i*300,
            mfe=0.001,
            mae=-0.01,
            strategy="TREND"
        ))
    
    m = analyzer.trade_metrics(rows)
    h1, h2 = analyzer.halves(rows)
    perf = analyzer.perf_by_strategy(rows)
    
    print(f"  metrics: n={m['n']}")
    
    rules = analyzer.adaptive_rules(m, h1, h2, get_param_mock, "TEST", perf)
    
    assert len(rules) == 0, f"Ожидалось 0 правил при n<10, получено: {len(rules)}"
    
    print("  ✓ Тест пройден\n")
    return True

def run_all_tests():
    """Запуск всех тестов."""
    print("=" * 60)
    print("TASK v13 PR1: Юнит-тесты adaptive_rules на синтетике")
    print("=" * 60 + "\n")
    
    tests = [
        test_sl_0_from_6,
        test_mfe_zero_strict_entry,
        test_gross_positive_net_negative,
        test_no_rules_when_n_less_than_10,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
        except AssertionError as e:
            print(f"  ✗ ПРОВАЛ: {e}\n")
            failed += 1
        except Exception as e:
            print(f"  ✗ ОШИБКА: {e}\n")
            failed += 1
    
    print("=" * 60)
    print(f"Итого: {passed} пройдено, {failed} провалено")
    print("=" * 60)
    
    return failed == 0

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
