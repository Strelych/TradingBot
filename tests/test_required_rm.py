from utils import compute_required_rm


def test_required_rm_basic():
    # Example: min_qty=1, price=100, balance=1000, margin_pct=0.1, leverage=10
    # required_notional = 1*100*1.2 = 120
    # denom = 1000 * 0.1 * 10 = 1000
    # required_rm = 120/1000 = 0.12
    rm = compute_required_rm(1.0, 100.0, 1000.0, 0.1, 10)
    assert abs(rm - 0.12) < 1e-6


def test_required_rm_impossible():
    # Make required_rm > 1.0 by raising min_qty
    rm = compute_required_rm(100.0, 100.0, 1000.0, 0.01, 5)
    assert rm > 1.0
