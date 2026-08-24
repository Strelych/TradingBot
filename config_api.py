# config_api.py - runtime-конфиг (v11.1)
import json, os
from fastapi import APIRouter, HTTPException

router = APIRouter()
_state = {"config": None, "path": "config_override.json"}
symbols_hook = None

CONFIG_META = {
    "leverage": {"group":"Риск и сайзинг","type":"int","min":1,"max":100,"step":1,"desc":"Плечо. Нотационная = маржа × плечо."},
    "margin_pct": {"group":"Риск и сайзинг","type":"float","min":0.01,"max":0.5,"step":0.01,"desc":"Маржа = баланс × margin_pct."},
    "max_risk_pct": {"group":"Риск и сайзинг","type":"float","min":0.001,"max":0.1,"step":0.001,"desc":"Гард: убыток на SL ≤ баланс × max_risk_pct."},
    "max_notional": {"group":"Риск и сайзинг","type":"float","min":10,"max":10000,"step":10,"desc":"Потолок нотационной на сделку."},
    "max_total_notional": {"group":"Риск и сайзинг","type":"float","min":100,"max":10000,"step":100,"desc":"Лимит суммарной нотационной."},
    "risk_mult": {"group":"Риск и сайзинг","type":"float","min":0.25,"max":2,"step":0.05,"desc":"Множитель риска пары (адаптер)."},
    "allowed_side": {"group":"Риск и сайзинг","type":"str","desc":"BOTH / Buy / Sell — разрешённая сторона."},
    "max_daily_trades": {"group":"Риск и сайзинг","type":"int","min":0,"max":500,"step":5,"desc":"0=без лимита сделок в день."},
    "max_daily_commission": {"group":"Риск и сайзинг","type":"float","min":0,"max":100,"step":0.5,"desc":"0=без лимита комиссий в день."},
    "daily_loss_halt_pct": {"group":"Риск и сайзинг","type":"float","min":0,"max":0.5,"step":0.01,"desc":"0=выкл; стоп входов при -X% баланса за день."},
    "wall_volume_multiplier": {"group":"WALL","type":"float","min":2,"max":50,"step":1,"desc":"Стена = объём > медиана × N."},
    "wall_min_age_seconds": {"group":"WALL","type":"int","min":5,"max":600,"step":5,"desc":"Мин. возраст стены."},
    "sl_behind_wall_pct": {"group":"WALL","type":"float","min":0.0001,"max":0.02,"step":0.0001,"desc":"SL за стеной."},
    "min_sl_distance_pct": {"group":"WALL","type":"float","min":0.001,"max":0.05,"step":0.001,"desc":"Пол дистанции SL."},
    "be_threshold_pct": {"group":"Выходы","type":"float","min":0,"max":0.05,"step":0.001,"desc":"Прибыль для безубытка."},
    "trail_activation_pct": {"group":"Выходы","type":"float","min":0,"max":0.1,"step":0.001,"desc":"Прибыль для старта трейлинга."},
    "trail_atr_mult": {"group":"Выходы","type":"float","min":0.5,"max":10,"step":0.5,"desc":"Трейлинг = ATR × N."},
    "trail_min_pct": {"group":"Выходы","type":"float","min":0.001,"max":0.05,"step":0.001,"desc":"Мин. отступ трейлинга."},
    "tp_atr_mult": {"group":"Выходы","type":"float","min":1,"max":10,"step":0.5,"desc":"TP = ATR × N (WALL)."},
    "min_tp_pct": {"group":"Выходы","type":"float","min":0.002,"max":0.1,"step":0.001,"desc":"Мин. дистанция TP."},
    "tp_round_number_preference": {"group":"Выходы","type":"bool","desc":"Снапить TP к круглому числу."},
    "grace_seconds": {"group":"Выходы","type":"float","min":0,"max":30,"step":1,"desc":"Не проверять выходы первые N сек."},
    "time_stop_seconds": {"group":"Выходы","type":"int","min":0,"max":172800,"step":60,"desc":"Тайм-стоп «мёртвых» сделок."},
    "trend_sl_atr_mult": {"group":"TREND","type":"float","min":0.5,"max":10,"step":0.5,"desc":"SL = ATR × N."},
    "trend_trail_atr_mult": {"group":"TREND","type":"float","min":0.5,"max":10,"step":0.5,"desc":"Трейлинг = ATR × N (после +1%)."},
    "trend_tp_pct": {"group":"TREND","type":"float","min":0.002,"max":0.05,"step":0.001,"desc":"Пол TP для TREND (доля)."},
    "trend_tp_atr_mult": {"group":"TREND","type":"float","min":0.5,"max":10,"step":0.5,"desc":"TP = ATR × N (если больше пола)."},
    "imbalance_threshold": {"group":"TREND","type":"float","min":0.1,"max":0.95,"step":0.05,"desc":"Порог дисбаланса стакана."},
    "imbalance_confirmation_ticks": {"group":"TREND","type":"int","min":1,"max":20,"step":1,"desc":"Тиков подтверждения дисбаланса."},
    "swing_sl_atr_mult": {"group":"SWING","type":"float","min":1,"max":10,"step":0.5,"desc":"SL = ATR(1h) × N."},
    "swing_tp_atr_mult": {"group":"SWING","type":"float","min":1,"max":15,"step":0.5,"desc":"TP = ATR(1h) × N."},
    "swing_time_stop": {"group":"SWING","type":"int","min":3600,"max":604800,"step":3600,"desc":"Тайм-стоп свинга."},
    "swing_risk_mult": {"group":"SWING","type":"float","min":0.1,"max":2,"step":0.05,"desc":"Множитель риска SWING."},
    "grid_levels": {"group":"GRID","type":"int","min":2,"max":10,"step":1,"desc":"Число уровней сетки."},
    "grid_step_pct": {"group":"GRID","type":"float","min":0.001,"max":0.05,"step":0.001,"desc":"Шаг сетки."},
    "grid_tp_mult": {"group":"GRID","type":"float","min":0.5,"max":3,"step":0.1,"desc":"TP уровня = шаг × N."},
    "grid_time_stop_seconds": {"group":"GRID","type":"int","min":60,"max":604800,"step":60,"desc":"Тайм-стоп GRID."},
    "allow_grid_with_position": {"group":"GRID","type":"bool","desc":"Разрешить GRID поверх позиции."},
    "breakout_sl_atr_mult": {"group":"BREAKOUT","type":"float","min":0.5,"max":10,"step":0.5,"desc":"SL = ATR × N."},
    "breakout_trail_atr_mult": {"group":"BREAKOUT","type":"float","min":1,"max":10,"step":0.5,"desc":"Трейлинг = ATR × N."},
    "breakout_cooldown_seconds": {"group":"BREAKOUT","type":"int","min":60,"max":86400,"step":60,"desc":"Пауза после BREAKOUT-сделки."},
    "breakout_time_stop": {"group":"BREAKOUT","type":"int","min":60,"max":172800,"step":60,"desc":"Тайм-стоп BREAKOUT."},
    "min_atr_pct_abs": {"group":"Фильтры","type":"float","min":0.0,"max":0.01,"step":0.0002,"desc":"Fee-floor: мин ATR для TREND/BREAKOUT."},
    "trading_hours_blacklist": {"group":"Фильтры","type":"list","desc":"Часы UTC через запятую, вход запрещён."},
    "max_spread_pct": {"group":"Фильтры","type":"float","min":0.0001,"max":0.01,"step":0.0001,"desc":"Макс. спред."},
    "atr_period": {"group":"Фильтры","type":"int","min":2,"max":200,"step":1,"desc":"Период ATR."},
    "require_trend_alignment": {"group":"Фильтры","type":"bool","desc":"Требовать совпадения тренда 1m+MTF."},
    "mtf_timeframes": {"group":"Фильтры","type":"list","desc":"Старшие ТФ через запятую."},
    "mtf_min_confirms": {"group":"Фильтры","type":"int","min":0,"max":2,"step":1,"desc":"Сколько старших ТФ подтверждают."},
    "loss_cooldown_seconds": {"group":"Фильтры","type":"int","min":0,"max":600,"step":10,"desc":"Пауза пары после убытка."},
    "min_atr_rel": {"group":"Фильтры","type":"float","min":0.1,"max":2,"step":0.1,"desc":"Мин. ATR к своей 24ч-медиане."},
    "max_atr_rel": {"group":"Фильтры","type":"float","min":1,"max":10,"step":0.5,"desc":"Макс. ATR к своей 24ч-медиане."},
    "adaptive_enabled": {"group":"Адаптер","type":"bool","desc":"Авто-тюнинг параметров пары."},
    "min_sample": {"group":"Адаптер","type":"int","min":5,"max":200,"step":5,"desc":"Мин. сделок за 72ч для решений."},
    "hysteresis": {"group":"Адаптер","type":"int","min":600,"max":86400,"step":600,"desc":"Пауза между сменами режимной стратегии."},
    "commission_maker": {"group":"Комиссии","type":"float","min":0,"max":0.01,"step":0.00001,"desc":"Maker."},
    "commission_taker": {"group":"Комиссии","type":"float","min":0,"max":0.01,"step":0.00001,"desc":"Taker."},
    "symbols": {"group":"Система","type":"list","desc":"Пары через запятую. Применяются БЕЗ перезапуска."},
}

def _coerce(meta, value):
    t = meta.get("type","float")
    try:
        if t=="bool": return bool(value)
        if t=="int": return int(value)
        if t=="float": return float(value)
        if t=="str": return str(value)
        if t=="list": return value if isinstance(value,list) else [x.strip() for x in str(value).split(",") if x.strip()]
        return value
    except Exception: return value

def init_config_api(config, path="config_override.json"):
    _state["config"]=config; _state["path"]=path
    if os.path.exists(path):
        try:
            ov=json.load(open(path,encoding="utf-8"))
            for k,v in ov.items():
                if k=="pair_overrides": config["pair_overrides"]=v
                elif k in CONFIG_META: config[k]=_coerce(CONFIG_META[k],v)
        except Exception as e: print("config load error:",e)

def _save():
    try:
        cfg=_state["config"]
        data={k:cfg.get(k) for k in CONFIG_META}
        data["pair_overrides"]=cfg.get("pair_overrides",{})
        json.dump(data,open(_state["path"],"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    except Exception as e: print("config save error:",e)

@router.get("/api/config")
def get_config():
    cfg=_state["config"]
    if cfg is None: raise HTTPException(500,"not initialized")
    return {k:dict(v,value=cfg.get(k)) for k,v in CONFIG_META.items()}

@router.post("/api/config")
async def set_config(payload: dict):
    cfg=_state["config"]
    if cfg is None: raise HTTPException(500,"not initialized")
    rr=[]
    for k,v in payload.items():
        m=CONFIG_META.get(k)
        if not m: raise HTTPException(400,f"unknown {k}")
        v=_coerce(m,v)
        if m["type"] in ("int","float"):
            if m.get("min") is not None and v<m["min"]: raise HTTPException(400,f"{k}: минимум {m['min']}")
            if m.get("max") is not None and v>m["max"]: raise HTTPException(400,f"{k}: максимум {m['max']}")
        cfg[k]=v
        if m.get("restart"): rr.append(k)
    _save()
    if "symbols" in payload and symbols_hook:
        await symbols_hook(cfg["symbols"])
    return {"ok":True,"restart_required":rr}