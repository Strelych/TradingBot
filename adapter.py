# adapter.py - независимый адаптивный движок (v11)
import asyncio, json, time, sqlite3
import analyzer

KN="knowledge.db"

class Adapter:
    def __init__(self):
        self.state=None;self.CONFIG=None;self.get_param=None;self.config_api=None
        self.kn_conn,self.kn_cur=self._init()
        self.last_n={};self.last_eval={};self.last_decision={};self.canary={};self.version=0
        # Для гистерезиса смен стратегии: хранит (candidate_strategy, count)
        self._streaks = {}
    def _init(self):
        conn=sqlite3.connect(KN,check_same_thread=False);c=conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS adaptive_log(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,
            symbol TEXT,param TEXT,old TEXT,new TEXT,reason TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS config_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,
            version INT,overrides TEXT,note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS strategy_performance(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,
            symbol TEXT,strategy TEXT,trades INT,wr REAL,gross REAL,fees REAL,net REAL,avg_mfe REAL,avg_mae REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS pair_knowledge(symbol TEXT PRIMARY KEY,ts REAL,strategy TEXT,
            risk_mult REAL,reason TEXT,locked INT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS live_regime(symbol TEXT PRIMARY KEY,ts REAL,vol_rel REAL,
            trendiness REAL,wall_share REAL,atr_pct REAL,bo_active INT,bo_side TEXT)""")
        conn.commit();return conn,c
    def start(self,state,CONFIG,get_param,config_api):
        self.state=state;self.CONFIG=CONFIG;self.get_param=get_param;self.config_api=config_api
        try:
            self.kn_cur.execute("SELECT MAX(version) FROM config_versions");r=self.kn_cur.fetchone();self.version=r[0] or 0
        except Exception:self.version=0
        asyncio.create_task(self.run())
    # --- публикация live-метрик (вызывает бот) ---
    def publish_regime(self,symbol,reg,bo):
        self.kn_cur.execute("INSERT OR REPLACE INTO live_regime(symbol,ts,vol_rel,trendiness,wall_share,atr_pct,bo_active,bo_side) VALUES(?,?,?,?,?,?,?,?)",
            (symbol,time.time(),reg.get("vol_rel",1),reg.get("trendiness",0),reg.get("wall_share",0),reg.get("atr_pct",0),1 if bo.get("active") else 0,bo.get("side") or ""))
    def read_regime(self,symbol):
        self.kn_cur.execute("SELECT vol_rel,trendiness,wall_share,atr_pct,bo_active,bo_side FROM live_regime WHERE symbol=?",(symbol,))
        r=self.kn_cur.fetchone()
        return r or (1.0,0.0,0.0,0.0,0,None)
    # --- журнал/версии/знания ---
    def log(self,symbol,param,old,new,reason):
        self.kn_cur.execute("INSERT INTO adaptive_log(ts,symbol,param,old,new,reason) VALUES(?,?,?,?,?,?)",
            (time.time(),symbol,param,str(old),str(new),reason))
    def bump(self,note):
        self.version+=1
        self.kn_cur.execute("INSERT INTO config_versions(ts,version,overrides,note) VALUES(?,?,?,?)",
            (time.time(),self.version,json.dumps(self.CONFIG.get("pair_overrides",{})),note))
    def save_knowledge(self,symbol,strat,rm,reason,locked):
        self.kn_cur.execute("INSERT OR REPLACE INTO pair_knowledge(symbol,ts,strategy,risk_mult,reason,locked) VALUES(?,?,?,?,?,?)",
            (symbol,time.time(),strat,rm,reason,1 if locked else 0))
    # --- оценка пары ---
    def eval(self,symbol,ov):
        rows=analyzer.get_rows(self.state.db_conn,symbol,72)
        m=analyzer.trade_metrics(rows)
        h1,h2=analyzer.halves(rows)
        scores=analyzer.strategy_scores(rows)
        lr=self.read_regime(symbol)
        reg={"vol_rel":lr[0],"trendiness":lr[1],"wall_share":lr[2]}
        bo={"active":bool(lr[4]),"side":lr[5]}
        last_trade=max((r[5] for r in rows),default=0)
        stale=(time.time()-last_trade)>2*3600
        locked=bool(ov.get("locked"))
        current=ov.get("adapter_strategy") or "AUTO"
        strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current,stale)

        # --- Холодный старт: если мало данных, но ATR явно велик, предпочесть TREND вместо WALL
        min_sample=self.CONFIG.get("min_sample",20)
        atr_pct=reg.get("atr_pct",0)
        atr_floor=self.CONFIG.get("min_atr_pct_abs",0.0016)
        atr_threshold=max(atr_floor*2,0.0025)
        if m.get("n",0)<min_sample and atr_pct>=atr_threshold and strat=="WALL":
            strat="TREND"
            rm=0.5
            reason=f"холодный старт: высокий ATR {atr_pct:.5f} -> TREND"

        # --- Не пиннить WALL в FLAT без стен: fallback в OFF (ожидание)
        if strat=="WALL" and reg.get("wall_share",0)<0.2:
            strat="OFF"
            rm=0.0
            reason=f"FLAT без стен (wall_share={reg.get('wall_share',0):.2f}) -> OFF"

        changes=[]
        # --- Гистерезис смен стратегии: применяем изменение только после 3 подряд рекомендаций
        if not locked:
            cur_as=ov.get("adapter_strategy")
            # обработка смены adapter_strategy с гистерезисом
            if cur_as!=strat:
                st=self._streaks.get(symbol, {"cand":None,"count":0})
                if st.get("cand")==strat:
                    st["count"]+=1
                else:
                    st={"cand":strat,"count":1}
                self._streaks[symbol]=st
                # если достигли порога — применяем изменение
                if st["count"]>=3:
                    changes.append(("adapter_strategy",cur_as,strat,reason))
                    # сбросим стрик после применения
                    self._streaks[symbol]={"cand":None,"count":0}
                else:
                    # не меняем пока не накопим стрик — обновим last_decision для наблюдаемости
                    self.last_decision[symbol]={"strategy":strat,"risk_mult":rm,"reason":f"hysteresis {st['count']}/3: {reason}","ts":time.time(),"locked":locked}
            else:
                # совпадение с текущей стратегией — сброс стрика
                self._streaks[symbol]={"cand":None,"count":0}

            # risk_mult и adaptive_rules применяются сразу (no hysteresis)
            if abs((ov.get("risk_mult") or 1.0)-rm)>1e-9:
                changes.append(("risk_mult",ov.get("risk_mult"),rm,reason))
            changes+=analyzer.adaptive_rules(m,h1,h2,self.get_param,symbol)

            # если есть изменения (включая adapter_strategy при достижении порога) — применяем
            if changes:
                for param,old,new,rsn in changes:
                    ov[param]=new; self.log(symbol,param,old,new,rsn)
                self.bump(f"adaptive {symbol}"); self.config_api._save()

        # Всегда сохраняем актуальное risk_mult/decision для полного отслеживания состояния
        self.canary[symbol]=rm
        # Если last_decision ещё не установлен выше in hysteresis branch, установим его
        if symbol not in self.last_decision:
            self.last_decision[symbol] = {"strategy":strat,"risk_mult":rm,"reason":reason,"ts":time.time(),"locked":locked}
        self.last_decision[symbol]["strategy"] = strat
        self.last_decision[symbol]["risk_mult"] = rm
        self.last_decision[symbol]["reason"] = self.last_decision[symbol].get("reason",reason)
        self.last_decision[symbol]["ts"] = time.time()

        self.save_knowledge(symbol,strat,rm,reason,locked)
        for s,pm in analyzer.perf_by_strategy(rows).items():
            self.kn_cur.execute("INSERT INTO strategy_performance(ts,symbol,strategy,trades,wr,gross,fees,net,avg_mfe,avg_mae) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(),symbol,s,pm["n"],pm["wr"],pm["gross"],pm["fees"],pm["net"],pm["avg_mfe"],pm["avg_mae"]))
    # --- цикл ---
    async def run(self):
        while True:
            await asyncio.sleep(60)
            try:
                for symbol in self.CONFIG["symbols"]:
                    ov=self.CONFIG.setdefault("pair_overrides",{}).setdefault(symbol,{})
                    self.state.db_cursor.execute("SELECT COUNT(*) FROM trades WHERE symbol=?",(symbol,))
                    n=self.state.db_cursor.fetchone()[0]
                    need_event=(n-self.last_n.get(symbol,n))>=10
                    if need_event or (time.time()-self.last_eval.get(symbol,0))>900:
                        self.eval(symbol,ov)
                        self.last_n[symbol]=n;self.last_eval[symbol]=time.time()
                self.kn_conn.commit()
            except Exception as e:
                print("adapter err:",e)
    # --- состояние для UI ---
    def public_state(self):
        self.kn_cur.execute("SELECT symbol,param,old,new,reason,ts FROM adaptive_log ORDER BY id DESC LIMIT 50")
        log=[{"symbol":r[0],"param":r[1],"old":r[2],"new":r[3],"reason":r[4],"ts":r[5]} for r in self.kn_cur.fetchall()]
        self.kn_cur.execute("SELECT version,note,ts FROM config_versions ORDER BY version DESC LIMIT 15")
        versions=[{"version":r[0],"note":r[1],"ts":r[2]} for r in self.kn_cur.fetchall()]
        pairs={}
        for s in self.CONFIG["symbols"]:
            ov=self.CONFIG.get("pair_overrides",{}).get(s,{})
            d=self.last_decision.get(s,{})
            pairs[s]={"set":ov.get("strategy") or "AUTO","adapter_strategy":ov.get("adapter_strategy"),
                "risk_mult":ov.get("risk_mult"),"locked":bool(ov.get("locked")),"canary":self.canary.get(s),
                "decision":d}
        return {"pairs":pairs,"log":log,"versions":versions,"version":self.version}

adapter=Adapter()