import analyzer


def test_recommend_trend():
    reg = {"atr_pct": 0.002, "trendiness": 0.2, "vol_rel": 1.0, "wall_share": 0.0}
    s, r = analyzer.recommend(reg)
    assert s == "TREND"


def test_recommend_wall():
    reg = {"atr_pct": 0.002, "trendiness": 0.1, "vol_rel": 1.0, "wall_share": 0.6}
    s, r = analyzer.recommend(reg)
    assert s == "WALL"


def test_recommend_swing():
    reg = {"atr_pct": 0.002, "trendiness": 0.05, "vol_rel": 0.6, "wall_share": 0.0}
    s, r = analyzer.recommend(reg)
    assert s == "SWING"


def test_recommend_fee_negative():
    reg = {"atr_pct": 0.0005, "trendiness": 0.3, "vol_rel": 1.5, "wall_share": 0.0}
    s, r = analyzer.recommend(reg)
    assert s == "FEE_NEGATIVE"


def test_decide_cold_start_uses_recommend_canary():
    scores = {}
    reg = {"atr_pct": 0.002, "trendiness": 0.2, "vol_rel": 1.0, "wall_share": 0.0}
    bo = {"active": False}
    # m indicates very small history
    m = {"n": 0}
    strat, rm, reason = analyzer.decide_strategy(scores, reg, bo, current="AUTO", stale=False, m=m)
    assert rm == 0.5
    assert strat in ("TREND", "SWING", "WALL", "FEE_NEGATIVE")


def test_decide_off_only_when_both_halves_bad():
    scores = {"TREND": {"n": 5, "net_per_trade": -0.05}}
    reg = {"atr_pct": 0.002}
    bo = {"active": False}
    m = {"n": 30}
    h1 = {"net_per_trade": -0.04}
    h2 = {"net_per_trade": -0.05}
    strat, rm, reason = analyzer.decide_strategy(scores, reg, bo, current="TREND", stale=False, m=m, h1=h1, h2=h2)
    assert strat == "OFF"
    assert rm == 0.0


def test_decide_profitable_prefers_best():
    scores = {"TREND": {"n": 12, "net_per_trade": 0.02}, "WALL": {"n": 6, "net_per_trade": 0.01}}
    reg = {"atr_pct": 0.002}
    bo = {"active": False}
    m = {"n": 30}
    strat, rm, reason = analyzer.decide_strategy(scores, reg, bo, current="AUTO", stale=False, m=m)
    assert strat == "TREND"
    assert rm == 1.0
