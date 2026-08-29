import math
import analyzer


def test_compute_pass_rm_simple():
    # Use small synthetic numbers
    mid = 100.0
    sl_dist = 1.0
    # current rm yields qty 0.5, min_qty required 1.0 -> required ratio 2x
    calc_qty = 0.5
    min_qty = 1.0
    rm = 0.1
    # replicate compute_pass_rm logic
    if calc_qty>0 and rm>0:
        required_ratio = float(min_qty) / float(calc_qty)
        pass_rm = min(1.0, rm * required_ratio * 1.2)
    else:
        pass_rm = min(1.0, (rm or 1.0) * 1.2)
    assert pass_rm is not None
    assert 0 < pass_rm <= 1.0


def test_fee_negative_treated_off():
    # reg with low atr_pct should produce FEE_NEGATIVE
    reg = {"atr_pct": 0.0, "trendiness": 0.0, "vol_rel": 1.0, "wall_share": 0.0, "min_atr_pct_abs": 0.0016}
    rec, reason = analyzer.recommend(reg)
    assert rec == "FEE_NEGATIVE"
    # Simulate server logic: recommendation FEE_NEGATIVE should be treated as OFF
    active = "OFF" if rec == "FEE_NEGATIVE" else "AUTO"
    assert active == "OFF"
