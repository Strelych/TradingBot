# server.py - Bybit Scalper v12 (Adaptive Analytics + Readiness + Hot Pairs)
import asyncio, json, time, sqlite3, os, math, traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import aiohttp, websockets, logging
from websockets.exceptions import ConnectionClosed
from datetime import datetime

from web_ui import WEB_PAGE
import config_api, analyzer, adapter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger=logging.getLogger("BotServer")

CONFIG={
 "virtual_balance":500.0,"leverage":10,
 "symbols":["BTCUSDT","ETHUSDT","SOLUSDT","HUSDT","GRAMUSDT"],
 "pair_overrides":{},
 "margin_pct":0.05,"max_risk_pct":0.01,"max_notional":1000.0,"max_total_notional":1500.0,
 "max_open_positions":3,"max_pending_orders":12,
 "max_daily_trades":0,"max_daily_commission":0.0,"daily_loss_halt_pct":0.0,
 "wall_volume_multiplier":8.0,"wall_min_age_seconds":60,"wall_persistence_check":5,
 "limit_order_offset_pct":0.0001,"order_timeout_seconds":60,
 "sl_behind_wall_pct":0.002,"min_sl_distance_pct":0.003,
 "be_threshold_pct":0.003,"trail_activation_pct":0.006,"trail_atr_mult":1.5,"trail_min_pct":0.005,
 "tp_atr_mult":3.0,"min_tp_pct":0.008,"tp_round_number_preference":True,
 "grace_seconds":2.0,"time_stop_seconds":900,
 "trend_sl_atr_mult":1.5,"trend_trail_atr_mult":2.0,"imbalance_threshold":0.6,"imbalance_confirmation_ticks":3,
 "trend_tp_pct":0.006,"trend_tp_atr_mult":2.0,
 "swing_sl_atr_mult":2.5,"swing_tp_atr_mult":4.0,"swing_time_stop":86400,"swing_risk_mult":0.75,
 "grid_levels":4,"grid_step_pct":0.004,"grid_tp_mult":1.2,
 "allow_grid_with_position":False,"grid_time_stop_seconds":43200,
 "breakout_lookback":24,"breakout_max_range_pct":0.06,"breakout_volume_mult":3.0,
 "breakout_sl_atr_mult":1.5,"breakout_trail_atr_mult":3.0,
 "breakout_cooldown_seconds":7200,"breakout_time_stop":21600,
 "min_atr_pct_abs":0.0016,"trading_hours_blacklist":[],
 "max_spread_pct":0.0006,"atr_period":14,
 "trend_price_tolerance_pct":0.005,
 "require_trend_alignment":True,"mtf_timeframes":["5","15"],"mtf_min_confirms":2,
 "adapter_hysteresis_count":3,"canary_fraction":0.25,
 "loss_cooldown_seconds":60,"min_atr_rel":0.4,"max_atr_rel":4.0,
 "adaptive_enabled":True,"min_sample":20,"hysteresis":21600,
 "commission_maker":0.00036,"commission_taker":0.001,
 "ema_period":20,"update_interval":0.2,"data_stale_threshold":5,
 "ws_ping_interval":20,"ws_ping_timeout":10,"bybit_base_url":"https://api.bybit.com",
}
config_api.init_config_api(CONFIG)

def get_param(symbol,key):
    """Получение параметра с приоритетом: override пары -> глобальный CONFIG -> None"""
    overrides = CONFIG.get("pair_overrides") or {}
    pair_override = overrides.get(symbol) or {}
    # Сначала ищем в override пары, потом в глобальном CONFIG
    if key in pair_override:
        return pair_override[key]
    return CONFIG.get(key)
def price_decimals(p):
    p=abs(p)
    if p>=1000:return 2
    if p>=100:return 3
    if p>=1:return 4
    if p>=0.01:return 6
    return 8
def round_price(p):return round(p,price_decimals(p))
def round_grid(p):
    if p>=1000:return 100
    if p>=100:return 10
    if p>=10:return 1
    if p>=1:return 0.1
    if p>=0.1:return 0.01
    return 0.001
def tp_to_round(p,side):
    g=round_grid(p)
    return math.ceil(p/g)*g if side=="Buy" else math.floor(p/g)*g
def median(v):
    if not v:return 0
    s=sorted(v);n=len(s)
    return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2

class BotState:
    def __init__(self):
        self.is_trading=False
        self.orderbooks={s:{"bids":{},"asks":{}} for s in CONFIG["symbols"]}
        self.last_prices={s:0.0 for s in CONFIG["symbols"]}
        self.last_update_time={s:0.0 for s in CONFIG["symbols"]}
        self.clients=set();self.start_time=None;self.bybit_ws=None
        self.db_conn=self.db_cursor=None;self.http_session=None
        self.kline_cache={};self.sym_info={};self.ws_connected=False
        self.support_resistance={s:{} for s in CONFIG["symbols"]}
        self.sr_cache_time={s:0.0 for s in CONFIG["symbols"]}
        self.current_trading_day=datetime.now().date()
        self.pending_orders=[];self.pending_id=1
        self.imbalance_counters={}
        self.cooldown_until={s:0.0 for s in CONFIG["symbols"]}
        self.tick_total={};self.tick_walls={};self.atr_hist={}
        self.recommended={};self.rec_reason={};self.last_switch={}
        self.daily_trades=0;self.daily_commission=0.0
        self.breakeven_trades=0
        self.last_tick=0.0;self.loop_errors=0;self.analysis_task=None
        self.stats={"virtual_balance":CONFIG["virtual_balance"],"initial_balance":CONFIG["virtual_balance"],
                    "total_trades":0,"winning_trades":0,"losing_trades":0,"total_pnl":0.0,"daily_pnl":0.0,"open_positions":{}}
        self.open_positions=self.stats["open_positions"]
    def sync_balance(self):self.stats["virtual_balance"]=paper_engine.balance
    def load_stats(self):
        try:
            self.db_cursor.execute("SELECT * FROM session_stats WHERE id=1");r=self.db_cursor.fetchone()
            if r:
                self.stats={"virtual_balance":r[2],"initial_balance":r[1],"total_trades":r[3],"winning_trades":r[4],
                            "losing_trades":r[5],"total_pnl":r[6],"daily_pnl":r[7],"start_time":r[8],"open_positions":{}}
                self.open_positions=self.stats["open_positions"]
            self.db_cursor.execute("SELECT COUNT(*) FROM trades WHERE gross_pnl>0 AND pnl<=0")
            self.breakeven_trades=self.db_cursor.fetchone()[0]
        except Exception as e:logger.error(f"load_stats:{e}")
    def save_stats(self):
        try:
            self.db_cursor.execute("""UPDATE session_stats SET current_balance=?,total_trades=?,winning_trades=?,
                losing_trades=?,total_pnl=?,daily_pnl=?,last_updated=? WHERE id=1""",
                (self.stats["virtual_balance"],self.stats["total_trades"],self.stats["winning_trades"],
                 self.stats["losing_trades"],self.stats["total_pnl"],self.stats["daily_pnl"],time.time()))
            self.db_conn.commit()
        except Exception as e:logger.error(f"save_stats:{e}")

state=BotState()

def has_position_for_symbol(symbol):
    return any(k==symbol or k.startswith(symbol+"#") for k in state.open_positions)
def has_plain_position(symbol):
    return symbol in state.open_positions
def total_notional():
    return sum(p["notional"] for p in state.open_positions.values())+sum(po.notional for po in state.pending_orders)
def eff_risk_mult(symbol,strat):
    v=get_param(symbol,"risk_mult")
    base=v if v is not None else 1.0
    canary_val=adapter.adapter.canary.get(symbol)
    # Если канарейка=None (пара еще не оценена), используем 1.0
    # Если канарейка=0.0 (OFF), блокируем торговлю возвращая 0
    # Если канарейка в диапазоне (0,1], используем её для снижения риска
    mult=canary_val if canary_val is not None else 1.0
    return base*mult

def init_db():
    conn=sqlite3.connect("market_data.db",check_same_thread=False);c=conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("""CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,symbol TEXT,price REAL,imbalance REAL,signal TEXT,trend TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL,
        symbol TEXT,side TEXT,entry_price REAL,exit_price REAL,qty REAL,pnl REAL,exit_reason TEXT,
        entry_trend TEXT,status TEXT,exit_timestamp REAL,initial_tp REAL,initial_sl REAL,margin_used REAL,
        gross_pnl REAL,wall_price REAL,strategy TEXT,spread_pct REAL,atr_pct REAL,imbalance REAL,wall_age REAL,
        mtf5 TEXT,mtf15 TEXT,mfe REAL,mae REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS session_stats(id INTEGER PRIMARY KEY CHECK(id=1),initial_balance REAL,
        current_balance REAL,total_trades INTEGER,winning_trades INTEGER,losing_trades INTEGER,
        total_pnl REAL,daily_pnl REAL,start_time REAL,last_updated REAL)""")
    for sql in ["ALTER TABLE trades ADD COLUMN mfe REAL","ALTER TABLE trades ADD COLUMN mae REAL"]:
        try:c.execute(sql)
        except Exception:pass
    c.execute("SELECT COUNT(*) FROM session_stats")
    if c.fetchone()[0]==0:
        c.execute("""INSERT INTO session_stats(id,initial_balance,current_balance,total_trades,winning_trades,
            losing_trades,total_pnl,daily_pnl,start_time,last_updated) VALUES(1,?,?,0,0,0,0,0,?,?)""",
            (CONFIG["virtual_balance"],CONFIG["virtual_balance"],time.time(),time.time()))
    conn.commit();return conn,c

async def init_http_session():state.http_session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
async def close_http_session():
    if state.http_session:await state.http_session.close()
async def fetch_json(url,params=None):
    if not state.http_session:return None
    try:
        async with state.http_session.get(url,params=params) as r:
            return await r.json() if r.status==200 else None
    except Exception:return None

async def get_symbol_info(symbol):
    now=time.time()
    if symbol in state.sym_info and now-state.sym_info[symbol][0]<3600:return state.sym_info[symbol][1]
    data=await fetch_json(f"{CONFIG['bybit_base_url']}/v5/market/instruments-info",{"category":"linear","symbol":symbol})
    info={}
    if data and data.get("result",{}).get("list"):
        it=data["result"]["list"][0]
        info={"qty_step":float(it["lotSizeFilter"]["qtyStep"]),"min_qty":float(it["lotSizeFilter"]["minOrderQty"]),
              "tick":float(it["priceFilter"]["tickSize"])}
    state.sym_info[symbol]=(now,info);return info
def round_qty(qty,info):
    step=info.get("qty_step",0.0001)
    return round(math.floor(qty/step)*step,6)
def round_tick(price,info):
    tick=info.get("tick",0.000001)
    return round(round(price/tick)*tick,price_decimals(price))

async def get_klines(symbol,interval,limit=100):
    key=(symbol,interval);now=time.time()
    if key in state.kline_cache and now-state.kline_cache[key][0]<15:return state.kline_cache[key][1]
    data=await fetch_json(f"{CONFIG['bybit_base_url']}/v5/market/kline",
        {"category":"linear","symbol":symbol,"interval":interval,"limit":limit})
    if not data or not data.get("result",{}).get("list"):return None
    kl=list(reversed(data["result"]["list"]))
    state.kline_cache[key]=(now,kl);return kl

def calc_atr(kl,period=14):
    if not kl or len(kl)<period+1:return 0
    tr=[]
    for i in range(1,len(kl)):
        h,l,pc=float(kl[i][2]),float(kl[i][3]),float(kl[i-1][4])
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(tr[-period:])/period

def ema_trend(closes,period=20,band=0.001):
    if not closes:return "UNKNOWN"
    e=closes[0]
    for c in closes[1:]:e=(c-e)*(2/(period+1))+e
    return "BULLISH" if closes[-1]>e*(1+band) else "BEARISH" if closes[-1]<e*(1-band) else "FLAT"

async def refresh_sr(symbol):
    if time.time()-state.sr_cache_time.get(symbol,0)<30:return state.support_resistance.get(symbol,{})
    kl=await get_klines(symbol,"1",100)
    if not kl:return {}
    closes=[float(k[4]) for k in kl]
    atr=calc_atr(kl,CONFIG["atr_period"]);atr_pct=atr/closes[-1] if closes[-1] else 0
    sr={"support":min(float(k[3]) for k in kl[-50:]),"resistance":max(float(k[2]) for k in kl[-50:]),
        "atr":atr,"atr_pct":atr_pct,"closes":closes}
    state.support_resistance[symbol]=sr;state.sr_cache_time[symbol]=time.time()
    h=state.atr_hist.setdefault(symbol,[]);h.append(atr_pct)
    if len(h)>288:state.atr_hist[symbol]=h[-288:]
    return sr

async def get_trend(symbol,interval):
    kl=await get_klines(symbol,interval,60)
    return ema_trend([float(k[4]) for k in kl]) if kl else "UNKNOWN"

class WallTracker:
    def __init__(self):self.walls={s:{} for s in CONFIG["symbols"]}
    def _scan(self,symbol,side,levels,mult):
        if not levels:return {}
        base=median(list(levels.values()))
        return {f"{side}_{p}":{"side":side,"price":p,"volume":v}
                for p,v in levels.items() if base>0 and v>base*mult}
    def update(self,symbol,ob):
        now=time.time();mult=get_param(symbol,"wall_volume_multiplier");cur={}
        cur.update(self._scan(symbol,"ask",ob["asks"],mult))
        cur.update(self._scan(symbol,"bid",ob["bids"],mult))
        st=self.walls[symbol]
        for k,w in cur.items():
            if k in st:
                st[k].update(volume=w["volume"],last_seen=now);st[k]["age"]=now-st[k]["first_seen"]
                st[k]["persistence"]=st[k].get("persistence",0)+1
            else:
                w.update(first_seen=now,last_seen=now,age=0,persistence=1);st[k]=w
        for k in list(st):
            if k not in cur and now-st[k]["last_seen"]>1.0:del st[k]
    def valid(self,symbol):
        return [w for w in self.walls[symbol].values()
                if w["age"]>=get_param(symbol,"wall_min_age_seconds")
                and w.get("persistence",0)>=get_param(symbol,"wall_persistence_check")]
wall_tracker=WallTracker()

class PendingOrder:
    def __init__(self,oid,symbol,side,price,qty,reason,margin,commission,notional,sl,tp,strategy,wall=None,grid_level=0,time_stop=None):
        self.order_id=oid;self.symbol=symbol;self.side=side;self.price=price;self.qty=qty;self.reason=reason
        self.margin=margin;self.commission=commission;self.notional=notional;self.sl=sl;self.tp=tp
        self.strategy=strategy;self.wall=wall;self.grid_level=grid_level;self.time_stop=time_stop
        self.created_at=time.time()
    def to_dict(self):
        return {"order_id":self.order_id,"symbol":self.symbol,"side":self.side,"price":round_price(self.price),
                "qty":self.qty,"reason":self.reason,"age_sec":round(time.time()-self.created_at,1),
                "margin":round(self.margin,2),"tp_price":round_price(self.tp),"sl_price":round_price(self.sl),
                "strategy":self.strategy,"grid_level":self.grid_level}

class PaperTradingEngine:
    def __init__(self,b):self.balance=b
    def place_limit_order(self,symbol,side,qty,price):
        notional=qty*price;commission=notional*CONFIG["commission_maker"];margin=notional/CONFIG["leverage"]
        if self.balance<margin+commission:return None
        self.balance-=(margin+commission)
        return {"notional":notional,"commission":commission,"margin":margin}
    def place_market_order(self,symbol,side,qty,price):
        notional=qty*price;commission=notional*CONFIG["commission_taker"];margin=notional/CONFIG["leverage"]
        if self.balance<margin+commission:return None
        self.balance-=(margin+commission)
        return {"notional":notional,"commission":commission,"margin":margin}
    def cancel_order(self,margin,commission):self.balance+=(margin+commission)
    def close_position(self,side,entry,qty,price,margin,entry_commission,is_maker):
        rate=CONFIG["commission_maker"] if is_maker else CONFIG["commission_taker"]
        exit_commission=qty*price*rate
        gross=(price-entry)*qty if side=="Buy" else (entry-price)*qty
        net=gross-entry_commission-exit_commission
        self.balance+=margin+gross-exit_commission
        return net,gross,exit_commission
paper_engine=PaperTradingEngine(CONFIG["virtual_balance"])

def compute_size(symbol,entry,sl_distance,rm):
    # Если rm=0 (канарейка OFF), возвращаем 0 - торговля заблокирована
    if rm==0 or entry==0:
        return 0.0
    balance=max(paper_engine.balance,1.0)
    margin=balance*get_param(symbol,"margin_pct")*rm
    notional=min(margin*CONFIG["leverage"],CONFIG["max_notional"])
    qty=notional/entry
    implied=notional*(sl_distance/entry) if entry else 0
    max_risk=balance*get_param(symbol,"max_risk_pct")
    if implied>max_risk and implied>0:qty*=max_risk/implied
    return qty

# ===== HOT ADD/REMOVE PAIRS (v12) =====
async def apply_symbols(new_list):
    wanted=[x.strip() for x in dict.fromkeys(new_list) if isinstance(x,str) and x.strip()]
    keep=[x for x in list(state.orderbooks.keys()) if x not in wanted and
          (has_position_for_symbol(x) or any(po.symbol==x for po in state.pending_orders))]
    if keep: logger.warning(f"⚠️ пары с открытыми позициями держим: {keep}")
    final=wanted+[k for k in keep if k not in wanted]
    old=set(CONFIG["symbols"]); new=set(final)
    CONFIG["symbols"]=final
    for x in new-old:
        state.orderbooks[x]={"bids":{},"asks":{}};state.last_prices[x]=0.0
        state.last_update_time[x]=0.0;state.cooldown_until[x]=0.0
        state.support_resistance.setdefault(x,{});state.sr_cache_time[x]=0.0
        state.atr_hist.setdefault(x,[]);state.tick_total[x]=0;state.tick_walls[x]=0
        wall_tracker.walls.setdefault(x,{})
        logger.info(f"➕ Пара добавлена: {x}")
    for x in old-new:
        for d in (state.orderbooks,state.last_prices,state.last_update_time,state.cooldown_until,
                  state.support_resistance,state.sr_cache_time,state.atr_hist,state.tick_total,
                  state.tick_walls,wall_tracker.walls): d.pop(x,None)
        logger.info(f"➖ Пара удалена: {x}")
    if state.bybit_ws:
        try:
            add=[f"orderbook.50.{x}" for x in new-old]+[f"publicTrade.{x}" for x in new-old]
            rem=[f"orderbook.50.{x}" for x in old-new]+[f"publicTrade.{x}" for x in old-new]
            if add: await state.bybit_ws.send(json.dumps({"op":"subscribe","args":add}))
            if rem: await state.bybit_ws.send(json.dumps({"op":"unsubscribe","args":rem}))
        except Exception as e: logger.error(f"WS resubscribe: {e}")
config_api.symbols_hook=apply_symbols

def finalize_close(key,pos,px,reason,is_maker=False):
    net,gross,exit_comm=paper_engine.close_position(pos["side"],pos["entry_price"],pos["qty"],px,
        pos["margin_used"],pos["entry_commission"],is_maker)
    state.stats["total_pnl"]+=net;state.stats["daily_pnl"]+=net;state.stats["total_trades"]+=1
    state.daily_trades+=1;state.daily_commission+=exit_comm
    if gross>0 and net<=0:state.breakeven_trades+=1
    if net>0:state.stats["winning_trades"]+=1
    else:
        state.stats["losing_trades"]+=1
        state.cooldown_until[pos["symbol"]]=time.time()+get_param(pos["symbol"],"loss_cooldown_seconds")
    if pos["strategy"]=="BREAKOUT":
        state.cooldown_until[pos["symbol"]]=max(state.cooldown_until.get(pos["symbol"],0),
            time.time()+get_param(pos["symbol"],"breakout_cooldown_seconds"))
    state.sync_balance()
    state.db_cursor.execute("""INSERT INTO trades(timestamp,symbol,side,entry_price,exit_price,qty,pnl,exit_reason,
        entry_trend,status,exit_timestamp,initial_tp,initial_sl,margin_used,gross_pnl,wall_price,strategy,
        spread_pct,atr_pct,imbalance,wall_age,mtf5,mtf15,mfe,mae) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pos["timestamp"],pos["symbol"],pos["side"],pos["entry_price"],px,pos["qty"],net,reason,pos.get("entry_trend",""),
         "closed",time.time(),pos["tp"],pos["sl"],pos["margin_used"],gross,pos.get("wall_price",0),pos["strategy"],
         pos.get("spread_pct",0),pos.get("atr_pct",0),pos.get("imbalance",0),pos.get("wall_age",0),
         pos.get("mtf5",""),pos.get("mtf15",""),pos.get("mfe",0),pos.get("mae",0)))
    state.db_conn.commit();state.save_stats()
    logger.info(f"📈 #{state.stats['total_trades']} {pos['symbol']} {pos['strategy']} {reason} Net ${net:+.2f} | Баланс ${paper_engine.balance:.2f}")
    del state.open_positions[key]

def shutdown_cleanup():
    for po in list(state.pending_orders):
        paper_engine.cancel_order(po.margin,po.commission);state.pending_orders.remove(po)
    for key in list(state.open_positions.keys()):
        pos=state.open_positions[key]
        ob=state.orderbooks.get(pos["symbol"],{})
        px=max(ob["bids"]) if (pos["side"]=="Buy" and ob.get("bids")) else (min(ob["asks"]) if ob.get("asks") else None)
        finalize_close(key,pos,px or pos["entry_price"],"SHUTDOWN_CLOSE")
    state.sync_balance()

@asynccontextmanager
async def lifespan(app):
    state.db_conn,state.db_cursor=init_db()
    await init_http_session()
    state.start_time=time.time();state.load_stats()
    paper_engine.balance=state.stats["virtual_balance"]
    asyncio.create_task(bybit_ws_handler())
    state.analysis_task=asyncio.create_task(analysis_loop())
    adapter.adapter.start(state,CONFIG,get_param,config_api)
    asyncio.create_task(housekeeping());asyncio.create_task(watchdog())
    logger.info("✅ Bybit Scalper v12 запущен")
    yield
    shutdown_cleanup()
    state.save_stats();await close_http_session()
    if state.db_conn:state.db_conn.close()

async def housekeeping():
    while True:
        await asyncio.sleep(30)
        try:
            if os.path.exists(config_api._state["path"]):
                ov=json.load(open(config_api._state["path"],encoding="utf-8"))
                old_syms=list(CONFIG["symbols"])
                for k,v in ov.items():
                    if k=="pair_overrides":CONFIG["pair_overrides"]=v
                    elif k=="symbols":pass
                    elif k in config_api.CONFIG_META:CONFIG[k]=config_api._coerce(config_api.CONFIG_META[k],v)
                if "symbols" in ov and list(ov["symbols"])!=old_syms:
                    await apply_symbols(ov["symbols"])
        except Exception:pass
        try:
            for symbol in CONFIG["symbols"]:
                reg=analyzer.regime(state,symbol)
                kl15=await get_klines(symbol,"15",60)
                bo=analyzer.breakout_state(kl15,get_param(symbol,"breakout_max_range_pct")) if kl15 else {"active":False,"side":None}
                adapter.adapter.publish_regime(symbol,reg,bo)
        except Exception:pass

async def watchdog():
    while True:
        await asyncio.sleep(5)
        if state.last_tick and time.time()-state.last_tick>15:
            logger.error("⚠️ Watchdog: цикл завис — перезапуск")
            state.loop_errors+=1
            if state.analysis_task and not state.analysis_task.done():state.analysis_task.cancel()
            state.analysis_task=asyncio.create_task(analysis_loop())
            state.last_tick=time.time()

app=FastAPI(title="Bybit Scalper v12",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
app.include_router(config_api.router)
@app.get("/",response_class=HTMLResponse)
async def web_ui_route():return WEB_PAGE

@app.post("/api/trading/start")
async def api_start():state.is_trading=True;logger.info("▶️ СТАРТ (REST)");return{"ok":True}
@app.post("/api/trading/stop")
async def api_stop():state.is_trading=False;logger.info("⏹️ СТОП (REST)");return{"ok":True}

@app.post("/api/close/{key}")
async def api_close(key:str):
    pos=state.open_positions.get(key)
    if not pos:
        for k,v in list(state.open_positions.items()):
            if k==key or k.startswith(key+"#"):pos=v;key=k;break
    if not pos:return{"ok":False,"error":"no position"}
    ob=state.orderbooks.get(pos["symbol"],{})
    if not ob.get("bids") or not ob.get("asks"):return{"ok":False,"error":"no data"}
    px=max(ob["bids"]) if pos["side"]=="Buy" else min(ob["asks"])
    finalize_close(key,pos,px,"MANUAL_CLOSE")
    return{"ok":True}

@app.post("/api/close_all")
async def api_close_all():
    n=0
    for key in list(state.open_positions.keys()):
        r=await api_close(key)
        if r["ok"]:n+=1
    return{"ok":True,"closed":n}

@app.post("/api/cancel_pending/{oid}")
async def api_cancel(oid:int):
    for po in list(state.pending_orders):
        if po.order_id==oid:
            paper_engine.cancel_order(po.margin,po.commission);state.sync_balance()
            state.pending_orders.remove(po)
            return{"ok":True}
    return{"ok":False}

@app.post("/api/reset")
async def api_reset():
    for t in ["trades","market_snapshots"]:
        state.db_cursor.execute(f"DELETE FROM {t}")
    state.db_cursor.execute("""UPDATE session_stats SET current_balance=?,total_trades=0,winning_trades=0,
        losing_trades=0,total_pnl=0,daily_pnl=0""",(CONFIG["virtual_balance"],))
    state.db_conn.commit()
    state.load_stats();paper_engine.balance=state.stats["virtual_balance"]
    logger.info("♻️ Полный сброс выполнен (knowledge.db сохранён)")
    return{"ok":True}

@app.get("/api/pairs")
async def api_pairs():
    out={}
    for s in CONFIG["symbols"]:
        reg=analyzer.regime(state,s)
        rec,reason=analyzer.recommend(reg)
        out[s]={"set":get_param(s,"strategy") or "AUTO","active":state.recommended.get(s,rec),"rec":rec,
                "rec_reason":state.rec_reason.get(s,reason),"regime":reg,
                "overrides":CONFIG.get("pair_overrides",{}).get(s,{})}
    return out

@app.post("/api/pairs/{symbol}")
async def api_pair_set(symbol:str,payload:dict):
    ov=CONFIG.setdefault("pair_overrides",{}).setdefault(symbol,{})
    ov.update(payload)
    if "strategy" in payload:ov["locked"]=(payload["strategy"]!="AUTO")
    config_api._save()
    logger.info(f"🎛️ {symbol} override: {payload}")
    return{"ok":True}

@app.get("/api/adapter")
async def api_adapter():
    return adapter.adapter.public_state()

@app.get("/api/analytics")
async def api_analytics():
    pairs={}
    for s in CONFIG["symbols"]:
        rows=analyzer.get_rows(state.db_conn,s,72)
        m=analyzer.trade_metrics(rows)
        perf=analyzer.perf_by_strategy(rows)
        hours=analyzer.hour_stats(rows)
        reg=analyzer.regime(state,s)
        rec,reason=analyzer.recommend(reg)
        kl15=await get_klines(s,"15",60)
        bo=analyzer.breakout_state(kl15,get_param(s,"breakout_max_range_pct")) if kl15 else {"active":False}
        pairs[s]={"metrics":m,"perf":perf,"hours":hours,"regime":reg,"recommendation":rec,"rec_reason":reason,
                  "breakout":bo,"progress":min(100,int(100*m.get("n",0)/max(1,get_param(s,"min_sample")))),
                  "set":get_param(s,"strategy") or "AUTO","active_strat":state.recommended.get(s,rec)}
    cur=state.db_cursor
    cur.execute("SELECT substr(datetime(exit_timestamp,'unixepoch','localtime'),1,10),SUM(pnl) FROM trades GROUP BY 1 ORDER BY 1 DESC LIMIT 14")
    daily=[{"date":r[0],"net":round(r[1],2)} for r in cur.fetchall()]
    return{"account":{"balance":round(state.stats["virtual_balance"],2),"total_pnl":round(state.stats["total_pnl"],2),
            "breakeven":state.breakeven_trades,"version":adapter.adapter.version},
           "pairs":pairs,"daily":daily}

async def bybit_ws_handler():
    while True:
        try:
            async with websockets.connect("wss://stream.bybit.com/v5/public/linear",
                ping_interval=CONFIG["ws_ping_interval"],ping_timeout=CONFIG["ws_ping_timeout"]) as ws:
                state.bybit_ws=ws
                await ws.send(json.dumps({"op":"subscribe","args":[f"orderbook.50.{s}" for s in CONFIG["symbols"]]+[f"publicTrade.{s}" for s in CONFIG["symbols"]]}))
                state.ws_connected=True
                logger.info(f"✅ WS подписан: {', '.join(CONFIG['symbols'])}")
                async for message in ws:
                    try:await process_bybit_message(json.loads(message))
                    except json.JSONDecodeError:pass
        except Exception as e:
            state.ws_connected=False;await asyncio.sleep(5)

async def process_bybit_message(message):
    if "topic" not in message:return
    topic,data,msg_type=message["topic"],message.get("data",{}),message.get("type","delta")
    if "orderbook" in topic:
        symbol=topic.split(".")[-1]
        if symbol not in state.orderbooks:return
        ob=state.orderbooks[symbol]
        if msg_type=="snapshot":
            ob["bids"]={float(p):float(v) for p,v in data.get("b",[]) if float(v)>0}
            ob["asks"]={float(p):float(v) for p,v in data.get("a",[]) if float(v)>0}
        else:
            for p,v in data.get("b",[]):
                pr,vo=float(p),float(v)
                if vo==0:ob["bids"].pop(pr,None)
                else:ob["bids"][pr]=vo
            for p,v in data.get("a",[]):
                pr,vo=float(p),float(v)
                if vo==0:ob["asks"].pop(pr,None)
                else:ob["asks"][pr]=vo
        state.last_update_time[symbol]=time.time();state.ws_connected=True
    elif "publicTrade" in topic and data:
        state.last_prices[topic.split(".")[-1]]=float(data[-1].get("p",0))

def entry_allowed(symbol):
    if CONFIG["max_daily_trades"]>0 and state.daily_trades>=CONFIG["max_daily_trades"]:return False
    if CONFIG["max_daily_commission"]>0 and state.daily_commission>=CONFIG["max_daily_commission"]:return False
    if CONFIG["daily_loss_halt_pct"]>0 and state.stats["daily_pnl"]<=-CONFIG["virtual_balance"]*CONFIG["daily_loss_halt_pct"]:return False
    if total_notional()>=CONFIG["max_total_notional"]:return False
    return True

async def entry_wall(symbol,ob,mid,best_bid,best_ask,sr,trend1,mtf,imbalance,atr,sinfo):
    out=[]
    need_side=None
    if imbalance>get_param(symbol,"imbalance_threshold"):
        state.imbalance_counters[symbol]=state.imbalance_counters.get(symbol,0)+1
        if state.imbalance_counters[symbol]>=get_param(symbol,"imbalance_confirmation_ticks"):need_side="Buy"
    elif imbalance<-get_param(symbol,"imbalance_threshold"):
        state.imbalance_counters[symbol]=state.imbalance_counters.get(symbol,0)-1
        if state.imbalance_counters[symbol]<=-get_param(symbol,"imbalance_confirmation_ticks"):need_side="Sell"
    else:state.imbalance_counters[symbol]=0
    if not need_side:
        logger.warning(f"⚠️ {symbol} entry_wall skipped: no imbalance/need_side")
        return out
    if get_param(symbol,"allowed_side") not in (None,"BOTH") and get_param(symbol,"allowed_side")!=need_side:
        logger.warning(f"⚠️ {symbol} entry_wall skipped: side not allowed (need {need_side})")
        return out
    if CONFIG["require_trend_alignment"]:
        need="BULLISH" if need_side=="Buy" else "BEARISH"
        if trend1!=need or sum(1 for tf in mtf.values() if tf==need)<get_param(symbol,"mtf_min_confirms"):
            logger.warning(f"⚠️ {symbol} entry_wall skipped: trend not aligned (need {need})")
            return out
    walls=[w for w in wall_tracker.valid(symbol) if w["side"]==("bid" if need_side=="Buy" else "ask")]
    if not walls:
        logger.warning(f"⚠️ {symbol} entry_wall skipped: no walls")
        return out
    wall=min(walls,key=lambda w:abs(w["price"]-mid))
    fp=best_ask if need_side=="Buy" else best_bid
    if abs(fp-mid)/mid>0.001:
        logger.warning(f"⚠️ {symbol} entry_wall skipped: price far from mid (fp={fp} mid={mid})")
        return out
    entry=round_tick(wall["price"]*(1+CONFIG["limit_order_offset_pct"]) if need_side=="Buy" else wall["price"]*(1-CONFIG["limit_order_offset_pct"]),sinfo)
    wall_sl=wall["price"]*(1-get_param(symbol,"sl_behind_wall_pct")) if need_side=="Buy" else wall["price"]*(1+get_param(symbol,"sl_behind_wall_pct"))
    floor=entry*(1-get_param(symbol,"min_sl_distance_pct")) if need_side=="Buy" else entry*(1+get_param(symbol,"min_sl_distance_pct"))
    sl=min(wall_sl,floor) if need_side=="Buy" else max(wall_sl,floor)
    sl_dist=abs(entry-sl)
    tp_d=max(atr*get_param(symbol,"tp_atr_mult"),entry*get_param(symbol,"min_tp_pct"))
    tp=entry+tp_d if need_side=="Buy" else entry-tp_d
    if get_param(symbol,"tp_round_number_preference"):
        r=tp_to_round(tp,need_side)
        if (need_side=="Buy" and r>entry) or (need_side=="Sell" and r<entry):tp=r
    qty=round_qty(compute_size(symbol,entry,sl_dist,eff_risk_mult(symbol,"WALL")),sinfo)
    if not qty or qty<sinfo.get("min_qty",0):
        logger.warning(f"⚠️ {symbol} entry_wall skipped: qty {qty} < min_qty {sinfo.get('min_qty',0)}")
        return out
    order=paper_engine.place_limit_order(symbol,need_side,qty,entry)
    if not order:
        logger.warning(f"⚠️ {symbol} entry_wall skipped: order placement failed")
        return out
    state.daily_commission+=order["commission"]
    out.append(PendingOrder(state.pending_id,symbol,need_side,entry,qty,
        f"Bounce от {wall['side']}-стены @ {round_price(wall['price'])}",order["margin"],order["commission"],
        order["notional"],round_price(sl),round_price(tp),"WALL",wall=wall))
    state.pending_id+=1
    return out

async def entry_trend(symbol,best_bid,best_ask,sr,trend1,mtf,imbalance,atr,sinfo):
    out=[]
    side=None
    if trend1=="BULLISH" and sum(1 for v in mtf.values() if v=="BULLISH")>=get_param(symbol,"mtf_min_confirms") and imbalance>-0.2:side="Buy"
    elif trend1=="BEARISH" and sum(1 for v in mtf.values() if v=="BEARISH")>=get_param(symbol,"mtf_min_confirms") and imbalance<0.2:side="Sell"
    if not side:
        logger.warning(f"⚠️ {symbol} entry_trend skipped: no side (trend/mtf/imbalance)")
        return out
    if get_param(symbol,"allowed_side") not in (None,"BOTH") and get_param(symbol,"allowed_side")!=side:
        logger.warning(f"⚠️ {symbol} entry_trend skipped: side not allowed (need {side})")
        return out
    closes=sr.get("closes",[])
    mid=(best_bid+best_ask)/2
    if closes:
        tol = get_param(symbol,"trend_price_tolerance_pct") or 0.005
        # Allow some leeway from recent highs/lows using configurable tolerance
        if side=="Buy" and mid<max(closes[-20:])*(1-tol):
            logger.warning(f"⚠️ {symbol} entry_trend skipped: price below recent highs")
            return out
        if side=="Sell" and mid>min(closes[-20:])*(1+tol):
            logger.warning(f"⚠️ {symbol} entry_trend skipped: price above recent lows")
            return out
    entry=round_tick(best_ask if side=="Buy" else best_bid,sinfo)
    sl_dist=atr*get_param(symbol,"trend_sl_atr_mult") if atr>0 else entry*0.005
    sl=entry-sl_dist if side=="Buy" else entry+sl_dist
    atr_pct=atr/entry if entry else 0
    tp_pct=max(get_param(symbol,"trend_tp_pct"),get_param(symbol,"trend_tp_atr_mult")*atr_pct)
    tp=round_price(entry*(1+tp_pct)) if side=="Buy" else round_price(entry*(1-tp_pct))
    qty=round_qty(compute_size(symbol,entry,sl_dist,eff_risk_mult(symbol,"TREND")),sinfo)
    if not qty or qty<sinfo.get("min_qty",0):
        logger.warning(f"⚠️ {symbol} entry_trend skipped: qty {qty} < min_qty {sinfo.get('min_qty',0)}")
        return out
    order=paper_engine.place_limit_order(symbol,side,qty,entry)
    if not order:
        logger.warning(f"⚠️ {symbol} entry_trend skipped: order placement failed")
        return out
    state.daily_commission+=order["commission"]
    out.append(PendingOrder(state.pending_id,symbol,side,entry,qty,f"Trend {trend1} + MTF",
        order["margin"],order["commission"],order["notional"],round_price(sl),tp,"TREND"))
    state.pending_id+=1
    return out

async def entry_swing(symbol,mid,sinfo):
    out=[]
    kl=await get_klines(symbol,"60",60)
    if not kl:
        logger.warning(f"⚠️ {symbol} entry_swing skipped: no klines 60")
        return out
    t60=ema_trend([float(k[4]) for k in kl])
    atr1h=calc_atr(kl,CONFIG["atr_period"])
    sup=min(float(k[3]) for k in kl[-50:]);res=max(float(k[2]) for k in kl[-50:])
    side=None;entry=None;sl=None;tp=None
    if t60=="BULLISH" and mid<=sup*(1+0.5*atr1h/sup):
        side="Buy";entry=sup*(1+0.0002);sl=entry-atr1h*get_param(symbol,"swing_sl_atr_mult");tp=entry+atr1h*get_param(symbol,"swing_tp_atr_mult")
    elif t60=="BEARISH" and mid>=res*(1-0.5*atr1h/res):
        side="Sell";entry=res*(1-0.0002);sl=entry+atr1h*get_param(symbol,"swing_sl_atr_mult");tp=entry-atr1h*get_param(symbol,"swing_tp_atr_mult")
    if not side:
        logger.warning(f"⚠️ {symbol} entry_swing skipped: no side (1h trend/zone)")
        return out
    if get_param(symbol,"allowed_side") not in (None,"BOTH") and get_param(symbol,"allowed_side")!=side:
        logger.warning(f"⚠️ {symbol} entry_swing skipped: side not allowed (need {side})")
        return out
    entry=round_tick(entry,sinfo);sl_dist=abs(entry-sl)
    qty=round_qty(compute_size(symbol,entry,sl_dist,eff_risk_mult(symbol,"SWING")*get_param(symbol,"swing_risk_mult")),sinfo)
    if not qty or qty<sinfo.get("min_qty",0):
        logger.warning(f"⚠️ {symbol} entry_swing skipped: qty {qty} < min_qty {sinfo.get('min_qty',0)}")
        return out
    order=paper_engine.place_limit_order(symbol,side,qty,entry)
    if not order:
        logger.warning(f"⚠️ {symbol} entry_swing skipped: order placement failed")
        return out
    state.daily_commission+=order["commission"]
    out.append(PendingOrder(state.pending_id,symbol,side,entry,qty,f"Swing 1h {t60} от зоны",
        order["margin"],order["commission"],order["notional"],round_price(sl),round_price(tp),"SWING",
        time_stop=get_param(symbol,"swing_time_stop")))
    state.pending_id+=1
    return out

async def entry_grid(symbol,mid,sinfo):
    out=[]
    if get_param(symbol,"allowed_side") not in (None,"BOTH","Buy"):
        logger.warning(f"⚠️ {symbol} entry_grid skipped: Buy not allowed")
        return out
    levels=get_param(symbol,"grid_levels");step=get_param(symbol,"grid_step_pct")
    open_grid=sum(1 for p in state.open_positions.values() if p["strategy"]=="GRID")
    pend_grid=sum(1 for po in state.pending_orders if po.strategy=="GRID")
    if open_grid+pend_grid>=levels:
        logger.warning(f"⚠️ {symbol} entry_grid skipped: grid levels exhausted ({open_grid+pend_grid}/{levels})")
        return out
    for i in range(1,levels+1):
        price=round_tick(mid*(1-i*step),sinfo)
        if any(abs(po.price-price)<price*0.0005 for po in state.pending_orders):continue
        if f"{symbol}#G{i}" in state.open_positions:continue
        sl=mid*(1-(levels+2)*step)
        sl_dist=abs(price-sl)
        qty=round_qty(compute_size(symbol,price,sl_dist,eff_risk_mult(symbol,"GRID"))/levels,sinfo)
        if not qty or qty<sinfo.get("min_qty",0):continue
        order=paper_engine.place_limit_order(symbol,"Buy",qty,price)
        if not order:
            logger.warning(f"⚠️ GRID {symbol}: не хватает средств на уровень {i}")
            continue
        state.daily_commission+=order["commission"]
        tp=price*(1+step*get_param(symbol,"grid_tp_mult"))
        out.append(PendingOrder(state.pending_id,symbol,"Buy",price,qty,f"GRID уровень -{i*step*100:.1f}%",
            order["margin"],order["commission"],order["notional"],round_price(sl),round_price(tp),"GRID",
            grid_level=i,time_stop=get_param(symbol,"grid_time_stop_seconds")))
        state.pending_id+=1
    return out

async def entry_breakout(symbol,bo,best_bid,best_ask,atr,sinfo):
    if not bo.get("active"):
        logger.warning(f"⚠️ {symbol} entry_breakout skipped: no breakout active")
        return None
    side=bo["side"]
    if get_param(symbol,"allowed_side") not in (None,"BOTH") and get_param(symbol,"allowed_side")!=side:
        logger.warning(f"⚠️ {symbol} entry_breakout skipped: side not allowed (need {side})")
        return None
    entry=best_ask if side=="Buy" else best_bid
    sl_dist=max(atr*get_param(symbol,"breakout_sl_atr_mult"),entry*0.006) if atr>0 else entry*0.01
    sl=entry-sl_dist if side=="Buy" else entry+sl_dist
    atr_pct=atr/entry if entry else 0
    tp_pct=max(get_param(symbol,"trend_tp_pct"),get_param(symbol,"trend_tp_atr_mult")*atr_pct)*2
    tp=round_price(entry*(1+tp_pct)) if side=="Buy" else round_price(entry*(1-tp_pct))
    qty=round_qty(compute_size(symbol,entry,sl_dist,eff_risk_mult(symbol,"BREAKOUT")),sinfo)
    if not qty or qty<sinfo.get("min_qty",0):
        logger.warning(f"⚠️ {symbol} entry_breakout skipped: qty {qty} < min_qty {sinfo.get('min_qty',0)}")
        return None
    order=paper_engine.place_market_order(symbol,side,qty,entry)
    if not order:
        logger.warning(f"⚠️ {symbol} entry_breakout skipped: market order failed")
        return None
    state.daily_commission+=order["commission"]
    return {"side":side,"entry":entry,"qty":qty,"order":order,"sl":round_price(sl),"tp":tp,
            "time_stop":get_param(symbol,"breakout_time_stop"),
            "reason":f"🚀 BREAKOUT диапазон {bo['range_pct']*100:.1f}% объём ×{bo['vol_ratio']}"}

def manage_position(pos,px,atr,now):
    symbol=pos["symbol"];side=pos["side"];entry=pos["entry_price"];qty=pos["qty"];strat=pos["strategy"]
    prof=(px-entry)/entry if side=="Buy" else (entry-px)/entry
    if prof>pos.get("highest",0):pos["highest"]=prof
    if prof>pos.get("mfe",0):pos["mfe"]=prof
    if prof<pos.get("mae",0):pos["mae"]=prof
    if strat=="TREND":
        be=(atr/entry) if atr>0 else 0.004;tact=max(0.01,be*1.5);tmult=get_param(symbol,"trend_trail_atr_mult");tmin=get_param(symbol,"trail_min_pct")
    elif strat=="SWING":
        be=0.005;tact=0.01;tmult=2.5;tmin=0.01
    elif strat=="GRID":
        be=0.0;tact=999;tmult=0;tmin=0
    elif strat=="BREAKOUT":
        be=0.0;tact=0.0;tmult=get_param(symbol,"breakout_trail_atr_mult");tmin=0.004
    else:
        be=get_param(symbol,"be_threshold_pct");tact=get_param(symbol,"trail_activation_pct")
        tmult=get_param(symbol,"trail_atr_mult");tmin=get_param(symbol,"trail_min_pct")
    if be>0 and prof>=be:
        ec=pos["entry_commission"];ex=px*qty*CONFIG["commission_taker"]
        be_off=(ec+ex)/qty/entry
        bep=entry*(1+be_off) if side=="Buy" else entry*(1-be_off)
        pos["sl"]=max(pos["sl"],round_price(bep)) if side=="Buy" else min(pos["sl"],round_price(bep))
    if tact<100 and prof>=tact and atr>0:
        td=max(px*tmin,atr*tmult)
        cand=round_price(px-td) if side=="Buy" else round_price(px+td)
        pos["sl"]=max(pos["sl"],cand) if side=="Buy" else min(pos["sl"],cand)
    if now-pos["timestamp"]<get_param(symbol,"grace_seconds"):return None
    if side=="Buy":
        if px<=pos["sl"]:return "TRAILING_STOP" if prof>0 else "STOP_LOSS"
        if pos["tp"]>0 and px>=pos["tp"]:return "TAKE_PROFIT"
    else:
        if px>=pos["sl"]:return "TRAILING_STOP" if prof>0 else "STOP_LOSS"
        if pos["tp"]>0 and px<=pos["tp"]:return "TAKE_PROFIT"
    ts=pos.get("time_stop") or get_param(symbol,"time_stop_seconds")
    if ts>0 and now-pos["timestamp"]>ts and prof<(be if be>0 else 0.003):return "TIME_STOP"
    return None

async def analysis_loop():
    while True:
        try:
            await asyncio.sleep(CONFIG["update_interval"])
            state.last_tick=time.time();now=time.time()
            payload={"type":"ticker_update","data":{}}
            db_records=[]
            today=datetime.now().date()
            if today!=state.current_trading_day:
                state.current_trading_day=today;state.stats["daily_pnl"]=0.0
                state.daily_trades=0;state.daily_commission=0.0
            for symbol in list(CONFIG["symbols"]):
                try:
                    ob=state.orderbooks.get(symbol)
                    if not ob or not ob["bids"] or not ob["asks"]:continue
                    data_age=now-state.last_update_time.get(symbol,0)
                    is_stale=data_age>CONFIG["data_stale_threshold"]
                    best_bid,best_ask=max(ob["bids"]),min(ob["asks"])
                    mid=(best_bid+best_ask)/2
                    spread_pct=(best_ask-best_bid)/mid if mid>0 else 1.0
                    tb=sum(ob["bids"][p] for p in sorted(ob["bids"],reverse=True)[:5])
                    ta=sum(ob["asks"][p] for p in sorted(ob["asks"])[:5])
                    imbalance=(tb-ta)/(tb+ta) if (tb+ta)>0 else 0
                    wall_tracker.update(symbol,ob)
                    valid=wall_tracker.valid(symbol)
                    state.tick_total[symbol]=state.tick_total.get(symbol,0)+1
                    if valid:state.tick_walls[symbol]=state.tick_walls.get(symbol,0)+1
                    sr=await refresh_sr(symbol)
                    atr=sr.get("atr",0);atr_pct=sr.get("atr_pct",0)
                    hist=state.atr_hist.get(symbol,[])
                    med=median(hist) if hist else atr_pct
                    vol_ok=(med>0 and get_param(symbol,"min_atr_rel")*med<=atr_pct<=get_param(symbol,"max_atr_rel")*med) if atr_pct else False
                    trend1=ema_trend(sr.get("closes",[]));sr["trend1"]=trend1
                    mtf={tf:await get_trend(symbol,tf) for tf in CONFIG["mtf_timeframes"]}
                    kl15=await get_klines(symbol,"15",60)
                    bo=analyzer.breakout_state(kl15,get_param(symbol,"breakout_max_range_pct")) if kl15 else {"active":False}
                    reg=analyzer.regime(state,symbol)
                    rec,reason=analyzer.recommend(reg)
                    if symbol not in state.recommended or now-state.last_switch.get(symbol,0)>CONFIG["hysteresis"]:
                        state.recommended[symbol]=rec;state.rec_reason[symbol]=reason
                    setv=get_param(symbol,"strategy") or "AUTO"
                    if setv=="AUTO":
                        active=get_param(symbol,"adapter_strategy") or state.recommended.get(symbol,rec)
                    else:active=setv
                    if setv=="AUTO" and bo.get("active"):active="BREAKOUT"
                    sinfo=await get_symbol_info(symbol)
                    # Оценка ожидаемого размера позиции для readiness (calc_qty)
                    calc_qty=0
                    skip_size=False
                    if active not in ("OFF",) and mid>0 and sinfo:
                        sl_dist=mid*0.01
                        rm=eff_risk_mult(symbol,active)
                        raw_qty=compute_size(symbol,mid,sl_dist,rm)
                        calc_qty=round_qty(raw_qty,sinfo)
                        # Если qty меньше min_qty — попытаться временно поднять risk_mult, если пара не locked
                        min_qty = sinfo.get("min_qty",0)
                        if calc_qty < min_qty:
                            ov = CONFIG.setdefault("pair_overrides",{}).setdefault(symbol,{})
                            if not ov.get("locked", False):
                                try:
                                    if calc_qty>0 and rm>0:
                                        required_ratio = float(min_qty) / float(calc_qty)
                                        pass_rm = min(1.0, rm * required_ratio * 1.2)
                                    else:
                                        pass_rm = min(1.0, (rm or 1.0) * 1.2)
                                    raw_qty2 = compute_size(symbol, mid, sl_dist, pass_rm)
                                    calc_qty2 = round_qty(raw_qty2, sinfo)
                                except Exception:
                                    calc_qty2 = 0; pass_rm = rm
                                if calc_qty2 >= min_qty:
                                    old_rm = ov.get("risk_mult")
                                    ov["risk_mult"] = pass_rm
                                    adapter.adapter.log(symbol, "risk_mult", old_rm, pass_rm, f"auto-size bump to pass min_qty ({calc_qty}->{calc_qty2})")
                                    config_api._save()
                                    calc_qty = calc_qty2
                                    rm = pass_rm
                                else:
                                    logger.warning(f"⚠️ {symbol} skip_size: qty {calc_qty} < min_qty {min_qty} even after bump to {pass_rm}")
                                    skip_size=True
                            else:
                                logger.warning(f"⚠️ {symbol} skip_size: locked override prevents size bump; qty {calc_qty} < min_qty {min_qty}")
                                skip_size=True
                    signal="HOLD"
                    in_cool=now<state.cooldown_until.get(symbol,0)
                    hour_ok=datetime.now().hour not in (get_param(symbol,"trading_hours_blacklist") or [])
                    gate=(state.is_trading and not is_stale and entry_allowed(symbol) and hour_ok
                        and spread_pct<=CONFIG["max_spread_pct"]
                        and len(state.open_positions)<CONFIG["max_open_positions"]
                        and len(state.pending_orders)<CONFIG["max_pending_orders"]
                        and paper_engine.balance>0)

                    # ===== ИНДИКАТОР ГОТОВНОСТИ (readiness) =====
                    checks=[]
                    def ck(l,ok):checks.append({"l":l,"ok":1 if ok else 0})
                    ck("торговля",state.is_trading);ck("стакан",not is_stale)
                    ck("спред",spread_pct<=CONFIG["max_spread_pct"]);ck("кулдаун",not in_cool)
                    ck("лимиты",entry_allowed(symbol));ck("час",hour_ok)
                    ck("слоты",len(state.open_positions)<CONFIG["max_open_positions"] and len(state.pending_orders)<CONFIG["max_pending_orders"])
                    ck("размер", calc_qty>=(sinfo.get("min_qty",0) if sinfo else 0))
                    miss=None
                    if not state.is_trading:miss="торговля выключена"
                    elif is_stale:miss="стакан устарел"
                    elif in_cool:miss="кулдаун %dс"%max(0,state.cooldown_until.get(symbol,0)-now)
                    elif not hour_ok:miss="час в блэклисте"
                    elif not entry_allowed(symbol):miss="лимиты дня/экспозиции"
                    elif spread_pct>CONFIG["max_spread_pct"]:miss="широкий спред"
                    if not(len(state.open_positions)<CONFIG["max_open_positions"] and len(state.pending_orders)<CONFIG["max_pending_orders"]):
                        miss="нет слотов"
                    # Логируем причину блокировки gate для диагностики
                    if not gate:
                        logger.debug(f"{symbol} gate blocked: {miss}")
                    else:
                        if active=="WALL":
                            ck("волатильность",vol_ok)
                            if not vol_ok:miss="волатильность вне диапазона"
                            else:
                                hw=len(valid)>0;ck("стена",hw)
                                if not hw:miss="нет подтверждённой стены"
                                else:
                                    need="Buy" if imbalance>get_param(symbol,"imbalance_threshold") else ("Sell" if imbalance<-get_param(symbol,"imbalance_threshold") else None)
                                    if need is None:ck("дисбаланс",0);miss="ждём дисбаланс"
                                    else:
                                        ck("дисбаланс",1);nt="BULLISH" if need=="Buy" else "BEARISH"
                                        tok=trend1==nt and sum(1 for v in mtf.values() if v==nt)>=get_param(symbol,"mtf_min_confirms")
                                        ck("тренд",tok)
                                        if not tok:miss="тренд: нужен %s"%nt
                                        else:
                                            sw=any(w["side"]==("bid" if need=="Buy" else "ask") for w in valid)
                                            ck("стена с нужной стороны",sw)
                                            miss=None if sw else "стена не с той стороны"
                        elif active=="TREND":
                            ck("волатильность",vol_ok);ck("fee-floor",atr_pct>=CONFIG["min_atr_pct_abs"])
                            if not vol_ok:miss="волатильность вне диапазона"
                            elif atr_pct<CONFIG["min_atr_pct_abs"]:miss="ATR ниже fee-floor"
                            else:
                                tok=trend1 in("BULLISH","BEARISH") and sum(1 for v in mtf.values() if v==trend1)>=get_param(symbol,"mtf_min_confirms")
                                ck("тренд",tok)
                                if not tok:miss="нет согласованного тренда"
                                else:
                                    ib=abs(imbalance)>=0.0;ck("дисбаланс",1)
                                    miss=None
                        elif active=="SWING":
                            kl60=await get_klines(symbol,"60",60)
                            t60=ema_trend([float(k[4]) for k in kl60]) if kl60 else "UNKNOWN"
                            ck("тренд 1h",t60 in("BULLISH","BEARISH"))
                            near=False
                            if kl60:
                                a1=calc_atr(kl60,CONFIG["atr_period"]);sup=min(float(k[3]) for k in kl60[-50:]);res=max(float(k[2]) for k in kl60[-50:])
                                near=(t60=="BULLISH" and mid<=sup*(1+0.5*a1/sup)) or (t60=="BEARISH" and mid>=res*(1-0.5*a1/res))
                            ck("у зоны",near)
                            miss=None if(t60 in("BULLISH","BEARISH") and near) else ("тренд 1h" if t60 not in("BULLISH","BEARISH") else "не у зоны S/R")
                        elif active=="GRID":
                            os_ok=get_param(symbol,"allowed_side") in(None,"BOTH","Buy");ck("Buy разрешён",os_ok)
                            miss=None if os_ok else "GRID: Buy запрещён"
                        elif active=="BREAKOUT":
                            ck("пробой",bo.get("active"));ck("fee-floor",atr_pct>=CONFIG["min_atr_pct_abs"])
                            miss=None if bo.get("active") else "нет пробоя диапазона"
                        elif active=="OFF":miss="пара выключена"
                    status_text="✅ готов" if miss is None else "⏳ "+miss

                    if gate and not in_cool:
                        has_pos=has_position_for_symbol(symbol)
                        has_pend=any(po.symbol==symbol for po in state.pending_orders)
                        new_pends=[]
                        if active=="BREAKOUT" and not has_pos and not has_pend and atr_pct>=CONFIG["min_atr_pct_abs"]:
                            res=await entry_breakout(symbol,bo,best_bid,best_ask,atr,sinfo)
                            if res:
                                state.open_positions[symbol]={"symbol":symbol,"side":res["side"],"entry_price":res["entry"],
                                    "qty":res["qty"],"timestamp":now,"atr":atr,"sl":res["sl"],"tp":res["tp"],"strategy":"BREAKOUT",
                                    "margin_used":res["order"]["margin"],"notional":res["order"]["notional"],
                                    "entry_commission":res["order"]["commission"],"wall_price":0,"wall_age":0,
                                    "entry_trend":trend1,"time_stop":res["time_stop"],"highest":0.0,"mfe":0.0,"mae":0.0,
                                    "spread_pct":spread_pct,"atr_pct":atr_pct,"imbalance":imbalance,
                                    "mtf5":mtf.get("5",""),"mtf15":mtf.get("15","")}
                                state.sync_balance();signal=res["side"].upper()
                                logger.info(f"🚀 BREAKOUT {res['side']} {res['qty']} {symbol} @ {round_price(res['entry'])}")
                        elif active=="WALL" and not has_pos and not has_pend and vol_ok:
                            new_pends=await entry_wall(symbol,ob,mid,best_bid,best_ask,sr,trend1,mtf,imbalance,atr,sinfo)
                        elif active=="TREND" and not has_pos and not has_pend and vol_ok and atr_pct>=CONFIG["min_atr_pct_abs"]:
                            new_pends=await entry_trend(symbol,best_bid,best_ask,sr,trend1,mtf,imbalance,atr,sinfo)
                        elif active=="SWING" and not has_pos and not has_pend:
                            new_pends=await entry_swing(symbol,mid,sinfo)
                        elif active=="GRID" and (CONFIG["allow_grid_with_position"] or not has_position_for_symbol(symbol)):
                            new_pends=await entry_grid(symbol,mid,sinfo)
                        for po in new_pends:
                            po.spread_pct=spread_pct;po.atr_pct=atr_pct;po.imbalance=imbalance
                            po.mtf5=mtf.get("5","");po.mtf15=mtf.get("15","")
                            state.pending_orders.append(po)
                            state.sync_balance();signal="PENDING"
                            logger.info(f"📋 LIMIT {po.side} {po.qty} {symbol} @ {round_price(po.price)} [{po.strategy}]")
                    for pending in list(state.pending_orders):
                        if pending.symbol!=symbol:continue
                        if has_position_for_symbol(symbol) and pending.strategy!="GRID":
                            paper_engine.cancel_order(pending.margin,pending.commission)
                            state.sync_balance();state.pending_orders.remove(pending)
                            continue
                        filled=(pending.side=="Buy" and best_ask<=pending.price) or (pending.side=="Sell" and best_bid>=pending.price)
                        if filled:
                            key=symbol if pending.strategy!="GRID" else f"{symbol}#G{pending.grid_level}"
                            state.open_positions[key]={
                                "symbol":symbol,"side":pending.side,"entry_price":pending.price,"qty":pending.qty,
                                "timestamp":now,"atr":atr,"sl":pending.sl,"tp":pending.tp,"strategy":pending.strategy,
                                "margin_used":pending.margin,"notional":pending.notional,"entry_commission":pending.commission,
                                "wall_price":pending.wall["price"] if pending.wall else 0,
                                "wall_age":pending.wall.get("age",0) if pending.wall else 0,
                                "entry_trend":trend1,"time_stop":pending.time_stop,"highest":0.0,"mfe":0.0,"mae":0.0,
                                "spread_pct":getattr(pending,"spread_pct",0),"atr_pct":getattr(pending,"atr_pct",0),
                                "imbalance":getattr(pending,"imbalance",0),"mtf5":getattr(pending,"mtf5",""),"mtf15":getattr(pending,"mtf15","")}
                            state.pending_orders.remove(pending);state.sync_balance()
                            logger.info(f"✅ FILL {pending.side} {pending.qty} {symbol} @ {round_price(pending.price)} [{pending.strategy}]")
                        elif now-pending.created_at>get_param(symbol,"order_timeout_seconds") and pending.strategy!="GRID":
                            paper_engine.cancel_order(pending.margin,pending.commission)
                            state.sync_balance();state.pending_orders.remove(pending)
                        elif now-pending.created_at>3600 and pending.strategy=="GRID":
                            paper_engine.cancel_order(pending.margin,pending.commission)
                            state.sync_balance();state.pending_orders.remove(pending)
                    for key in list(state.open_positions):
                        pos=state.open_positions[key]
                        if pos["symbol"]!=symbol:continue
                        if atr>0:pos["atr"]=atr
                        px=best_bid if pos["side"]=="Buy" else best_ask
                        reason=manage_position(pos,px,pos["atr"],now)
                        if reason:
                            finalize_close(key,pos,px,reason,is_maker=False)
                    walls_out=[{"side":w["side"],"price":round_price(w["price"]),"volume":round(w["volume"],1),"age":int(w["age"])} for w in valid[:2]]
                    # Расчет потенциального размера позиции для UI
                    calc_qty=0
                    if active not in("OFF",) and mid>0:
                        sl_dist=mid*0.01  # примерный SL для оценки
                        rm=eff_risk_mult(symbol,active)
                        raw_qty=compute_size(symbol,mid,sl_dist,rm)
                        calc_qty=round_qty(raw_qty,sinfo) if sinfo else 0
                    
                    payload["data"][symbol]={"price":round_price(mid),"imbalance":round(imbalance,3),"signal":signal,
                        "trend":trend1,"is_trading":state.is_trading,"last_price":round_price(state.last_prices.get(symbol,mid)),
                        "spread_pct":round(spread_pct,5),"atr_pct":round(atr_pct,5),"valid_walls":len(valid),"walls":walls_out,
                        "cooldown":int(max(0,state.cooldown_until.get(symbol,0)-now)),"breakout":bo,
                        "strat_set":setv,"strat_active":active,"rec_reason":state.rec_reason.get(symbol,reason),"regime":reg,
                        "status":status_text,"checks":checks,"canary":adapter.adapter.canary.get(symbol,0),
                        "min_qty":sinfo.get("min_qty",0),"calc_qty":calc_qty}
                    db_records.append((time.time(),symbol,mid,imbalance,signal,trend1))
                except Exception as e:
                    state.loop_errors+=1
                    logger.error(f"Ошибка по {symbol}: {e}\n{traceback.format_exc()}")
            if db_records and state.db_cursor:
                try:
                    state.db_cursor.executemany("INSERT INTO market_snapshots(timestamp,symbol,price,imbalance,signal,trend) VALUES(?,?,?,?,?,?)",db_records)
                    state.db_conn.commit()
                except Exception as e:logger.error(f"DB:{e}")
            if state.clients and payload["data"]:
                opd=[]
                for key,p in state.open_positions.items():
                    s=p["symbol"]
                    if s in state.orderbooks and state.orderbooks[s]["bids"] and state.orderbooks[s]["asks"]:
                        px=max(state.orderbooks[s]["bids"]) if p["side"]=="Buy" else min(state.orderbooks[s]["asks"])
                        unrl=(px-p["entry_price"])*p["qty"] if p["side"]=="Buy" else (p["entry_price"]-px)*p["qty"]
                        opd.append({"key":key,"symbol":s,"side":p["side"],"entry_price":p["entry_price"],
                            "current_price":round_price(px),"qty":p["qty"],"current_pnl":round(unrl,2),
                            "tp_price":p["tp"],"sl_price":p["sl"],"strategy":p["strategy"],
                            "entry_time":p["timestamp"],"hold_time_sec":round(now-p["timestamp"],1),
                            "margin_used":p["margin_used"],"notional":p["notional"]})
                st=dict(state.stats);st["breakeven"]=state.breakeven_trades
                payload["open_positions"]=opd
                payload["pending_orders"]=[po.to_dict() for po in state.pending_orders]
                payload["stats"]=st
                payload["health"]={"last_tick_age":round(time.time()-state.last_tick,2),
                    "uptime_sec":int(time.time()-(state.start_time or time.time())),"loop_errors":state.loop_errors}
                msg=json.dumps(payload);dead=set()
                for cl in list(state.clients):
                    try:await cl.send_text(msg)
                    except Exception:dead.add(cl)
                state.clients-=dead
        except asyncio.CancelledError:raise
        except Exception as e:
            state.loop_errors+=1
            logger.error(f"analysis_loop:{e}\n{traceback.format_exc()}")

@app.websocket("/ws")
async def websocket_endpoint(websocket:WebSocket):
    await websocket.accept();state.clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type":"state","is_trading":state.is_trading,"stats":state.stats}))
        while True:
            cmd=json.loads(await websocket.receive_text())
            if cmd.get("action")=="start":state.is_trading=True;logger.info("▶️ СТАРТ (WS)")
            elif cmd.get("action")=="stop":state.is_trading=False;logger.info("⏹️ СТОП (WS)")
            await websocket.send_text(json.dumps({"type":"ack","action":cmd.get("action")}))
    except WebSocketDisconnect:
        state.clients.remove(websocket)

@app.get("/api/status")
async def get_status():
    return{"is_trading":state.is_trading,"symbols":CONFIG["symbols"],"ws_connected":state.ws_connected,
           "stats":state.stats,"health":{"last_tick_age":round(time.time()-state.last_tick,2),"loop_errors":state.loop_errors}}

@app.get("/api/trades")
async def get_trades(limit:int=1000):
    try:
        state.db_cursor.execute("""SELECT timestamp,symbol,side,entry_price,exit_price,qty,pnl,exit_reason,
            entry_trend,exit_timestamp,initial_tp,initial_sl,margin_used,gross_pnl,wall_price,strategy,mfe,mae
            FROM trades ORDER BY id DESC LIMIT ?""",(limit,))
        return[{"timestamp":r[0],"symbol":r[1],"side":r[2],"entry_price":r[3],"exit_price":r[4],"qty":r[5],"pnl":r[6],
                "exit_reason":r[7],"entry_trend":r[8],"exit_timestamp":r[9],"initial_tp":r[10],"initial_sl":r[11],
                "margin_used":r[12],"gross_pnl":r[13],"wall_price":r[14],"strategy":r[15],"mfe":r[16],"mae":r[17]} for r in state.db_cursor.fetchall()]
    except Exception:return[]

if __name__=="__main__":
    import uvicorn;uvicorn.run(app,host="0.0.0.0",port=8000,log_level="info")