def compute_required_rm(min_qty, price, balance, margin_pct, leverage):
    """Deterministic required risk_mult to satisfy min_qty at price.
    required_notional = min_qty * price * 1.2
    required_rm = required_notional / (balance * margin_pct * leverage)
    """
    try:
        balance = float(balance)
        margin_pct = float(margin_pct)
        leverage = float(leverage)
    except Exception:
        return float('inf')
    if balance<=0 or margin_pct<=0 or leverage<=0:
        return float('inf')
    required_notional = float(min_qty) * float(price) * 1.2
    required_rm = required_notional / (balance * margin_pct * leverage)
    return required_rm
