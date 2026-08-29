# config_api.py - runtime-конфиг (v11.1)
import json
import os
import logging
import threading
from copy import deepcopy
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()
_state = {"config": None, "path": "config_override.json", "lock": threading.Lock()}
symbols_hook = None

CONFIG_META: Dict[str, Dict[str, Any]] = {
    "limit_order_offset_pct": {"group":"WALL","type":"float","min":0.0,"max":0.01,"step":0.0001,"desc":"Смещение лимитного ордера от стены."},
    "order_timeout_seconds": {"group":"WALL","type":"int","min":10,"max":600,"step":10,"desc":"Таймаут отмены лимитного ордера."},
    "breakout_lookback": {"group":"BREAKOUT","type":"int","min":10,"max":100,"step":5,"desc":"Свечей для анализа диапазона."},
    "breakout_max_range_pct": {"group":"BREAKOUT","type":"float","min":0.01,"max":0.2,"step":0.01,"desc":"Макс. % диапазона пробоя."},
    "breakout_volume_mult": {"group":"BREAKOUT","type":"float","min":1,"max":20,"step":0.5,"desc":"Множитель объёма для пробоя."},
    "ema_period": {"group":"Фильтры","type":"int","min":5,"max":100,"step":1,"desc":"Период EMA для тренда."},
    "update_interval": {"group":"Система","type":"float","min":0.05,"max":2,"step":0.05,"desc":"Интервал цикла анализа (сек)."},
    "data_stale_threshold": {"group":"Система","type":"int","min":1,"max":30,"step":1,"desc":"Порог устаревания данных (сек)."},
    "ws_ping_interval": {"group":"Система","type":"int","min":5,"max":60,"step":5,"desc":"Ping WS интервал."},
    "ws_ping_timeout": {"group":"Система","type":"int","min":5,"max":30,"step":1,"desc":"Ping WS таймаут."},
    "bybit_base_url": {"group":"Система","type":"str","desc":"Базовый URL Bybit API."},
    "virtual_balance": {"group":"Система","type":"float","min":10,"max":100000,"step":10,"desc":"Виртуальный баланс (paper trading)."},
    "leverage": {"group":"Риск и сайзинг","type":"int","min":1,"max":100,"step":1,"desc":"Плечо. Нотационная = маржа × плечо."},
    "margin_pct": {"group":"Риск и сайзинг","type":"float","min":0.01,"max":0.5,"step":0.01,"desc":"Маржа = баланс × margin_pct."},
    "max_risk_pct": {"group":"Риск и сайзинг","type":"float","min":0.001,"max":0.1,"step":0.001,"desc":"Гард: убыток на SL ≤ баланс × max_risk_pct."},
    "max_notional": {"group":"Риск и сайзинг","type":"float","min":10,"max":10000,"step":10,"desc":"Потолок нотационной на сделку."},
    "max_total_notional": {"group":"Риск и сайзинг","type":"float","min":100,"max":10000,"step":100,"desc":"Лимит суммарной нотационной."},
    "risk_mult": {"group":"Риск и сайзинг","type":"float","min":0.25,"max":2,"step":0.05,"desc":"Множитель риска пары (адаптер)."},
    "adapter_strategy": {"group":"Адаптер","type":"str","desc":"Стратегия адаптера: WALL/TREND/SWING/GRID/BREAKOUT/OFF/AUTO."},
    "strategy": {"group":"Адаптер","type":"str","desc":"Ручная стратегия пары: WALL/TREND/SWING/GRID/BREAKOUT/OFF/AUTO."},
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
    "adapter_hysteresis_count": {"group":"Адаптер","type":"int","min":1,"max":10,"step":1,"desc":"Число циклов для гистерезиса смен стратегии."},
    "canary_fraction": {"group":"Адаптер","type":"float","min":0.01,"max":1.0,"step":0.01,"desc":"Фракция риска для canary при малом sample (0.25 = 25%)."},
    "canary_ramp_wins": {"group":"Адаптер","type":"int","min":1,"max":10,"step":1,"desc":"Число прибыльных сделок в окне для восстановления риска после canary."},
    "canary_ramp_window": {"group":"Адаптер","type":"int","min":1,"max":10,"step":1,"desc":"Окно последних сделок для проверки canary ramp."},
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

def _coerce(meta: Dict[str, Any], value: Any) -> Any:
    """Преобразует значение к типу, указанному в мета-описании."""
    t = meta.get("type", "float")
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes", "on")
            return bool(value)
        if t == "int":
            return int(float(value))  # Позволяет "10.0" → 10
        if t == "float":
            return float(value)
        if t == "str":
            return str(value) if value is not None else ""
        if t == "list":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [x.strip() for x in value.split(",") if x.strip()]
            return [value] if value is not None else []
        return value
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Coerce error for {meta.get('desc', 'unknown')}: {e}")
        return value


def init_config_api(config: Dict[str, Any], path: str = "config_override.json") -> None:
    """Инициализирует конфигурацию и загружает overrides из файла."""
    with _state["lock"]:
        _state["config"] = config
        _state["path"] = path
    
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            for k, v in ov.items():
                if k == "pair_overrides":
                    config["pair_overrides"] = v
                elif k in CONFIG_META:
                    config[k] = _coerce(CONFIG_META[k], v)
            logger.info(f"Config loaded from {path}: {len(ov)} keys")
        except json.JSONDecodeError as e:
            logger.error(f"Config JSON decode error ({path}): {e}")
        except Exception as e:
            logger.error(f"Config load error ({path}): {e}")


def _save() -> bool:
    """Сохраняет текущую конфигурацию в файл overrides."""
    try:
        cfg = _state["config"]
        if cfg is None:
            return False
        data = {k: cfg.get(k) for k in CONFIG_META}
        data["pair_overrides"] = cfg.get("pair_overrides", {})
        with open(_state["path"], "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Config saved to {_state['path']}")
        return True
    except Exception as e:
        logger.error(f"Config save error: {e}")
        return False


@router.get("/api/config")
def get_config() -> Dict[str, Any]:
    """Возвращает все параметры конфигурации с метаданными и текущими значениями."""
    cfg = _state["config"]
    if cfg is None:
        raise HTTPException(status_code=500, detail="Configuration not initialized")
    
    result = {}
    with _state["lock"]:
        for k, v in CONFIG_META.items():
            item = dict(v)
            item["value"] = cfg.get(k)
            result[k] = item
    return result


@router.post("/api/config")
async def set_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Обновляет параметры конфигурации. Возвращает список ключей, требующих перезапуска."""
    cfg = _state["config"]
    if cfg is None:
        raise HTTPException(status_code=500, detail="Configuration not initialized")
    
    restart_required: List[str] = []
    errors: List[str] = []
    
    with _state["lock"]:
        for k, v in payload.items():
            meta = CONFIG_META.get(k)
            if not meta:
                errors.append(f"Unknown parameter: {k}")
                continue
            
            v_coerced = _coerce(meta, v)
            
            # Валидация числовых диапазонов
            if meta["type"] in ("int", "float"):
                min_val = meta.get("min")
                max_val = meta.get("max")
                if min_val is not None and v_coerced < min_val:
                    errors.append(f"{k}: minimum value is {min_val}, got {v_coerced}")
                    continue
                if max_val is not None and v_coerced > max_val:
                    errors.append(f"{k}: maximum value is {max_val}, got {v_coerced}")
                    continue
            
            cfg[k] = v_coerced
            if meta.get("restart"):
                restart_required.append(k)
        
        # Сохранение после успешной валидации всех параметров
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        
        _save()
    
    # Вызов хука для обновления символов (вне блокировки)
    if "symbols" in payload and symbols_hook:
        try:
            await symbols_hook(cfg["symbols"])
        except Exception as e:
            logger.error(f"Symbols hook error: {e}")
    
    return {"ok": True, "restart_required": restart_required}