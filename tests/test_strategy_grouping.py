# test_strategy_grouping.py - юнит-тест для PR1 (TASK_FIX_TRADE)
import sqlite3
import sys
sys.path.insert(0, '/workspace')
import analyzer

def test_strategy_grouping():
    """Проверка: strategy_scores ключи ⊆ {WALL,TREND,SWING,GRID,BREAKOUT}"""
    db = sqlite3.connect('/workspace/market_data.db')
    
    # Создадим тестовые данные если их нет
    cur = db.cursor()
    cur.execute('SELECT COUNT(*) FROM trades WHERE symbol=?', ('AKEUSDT',))
    if cur.fetchone()[0] == 0:
        test_data = [
            (1.5, 2.0, 'TAKE_PROFIT', 'Buy', 1100, 1000, 2.5, 0.1, 'TREND'),
            (-0.8, 0.5, 'STOP_LOSS', 'Buy', 1101, 1001, 0.2, 0.8, 'TREND'),
            (0.7, 1.0, 'TAKE_PROFIT', 'Sell', 1102, 1002, 1.2, 0.3, 'WALL'),
            (-0.3, 0.2, 'TRAILING_STOP', 'Buy', 1103, 1003, 0.5, 0.3, 'SWING'),
            (2.0, 2.5, 'TAKE_PROFIT', 'Buy', 1104, 1004, 3.0, 0.2, 'GRID'),
            (-1.0, 0.3, 'STOP_LOSS', 'Sell', 1105, 1005, 0.1, 1.0, 'BREAKOUT'),
            (0.5, 0.8, 'TAKE_PROFIT', 'Buy', 1106, 1006, 1.0, 0.2, 'INVALID_STRAT'),
            (-0.5, 0.3, 'TAKE_PROFIT', 'Buy', 1107, 1007, 0.4, 0.5, 'TAKE_PROFIT'),
        ]
        for d in test_data:
            cur.execute('INSERT INTO trades(timestamp,symbol,side,entry_price,exit_price,qty,pnl,exit_reason,status,gross_pnl,strategy,mfe,mae,exit_timestamp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (d[5], 'AKEUSDT', d[3], 100, 101, 1, d[0], d[2], 'closed', d[1], d[8], d[6], d[7], d[4]))
        db.commit()
    
    rows = analyzer.get_rows(db, 'AKEUSDT', 240)
    scores = analyzer.strategy_scores(rows)
    VALID = {"WALL", "TREND", "SWING", "GRID", "BREAKOUT"}
    
    # Проверка whitelist
    assert set(scores.keys()).issubset(VALID), f"FAIL: keys={scores.keys()}, expected subset of {VALID}"
    
    # Проверка что exit_reason leak отфильтрован
    assert 'TAKE_PROFIT' not in scores, f"FAIL: TAKE_PROFIT не должен быть ключом (это exit_reason)"
    assert 'INVALID_STRAT' not in scores, f"FAIL: INVALID_STRAT отфильтрован whitelist"
    
    print(f"✅ grouping OK: keys={sorted(scores.keys())}")
    return True

if __name__ == "__main__":
    test_strategy_grouping()
