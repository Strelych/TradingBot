# analyzer.py - аналитический движок (v11)
import time, statistics
from datetime import datetime

def trendiness(closes):
    if len(closes)<30: return 0.0
    r=[(closes[i]-closes[i-1])/closes[i-1] for i in range(1,len(closes))]
    den=sum(abs(x) for x in r)
    return abs(sum(r))/den if den>0 else 0.0

def regime(state, symbol):
    sr=state.support_resistance.get(symbol,{})
    atr_pct=sr.get("atr_pct",0)
    hist=state.atr_hist.get(symbol,[])
    med=statistics.median(hist) if hist else (atr_pct or 0.001)
    vol_rel=atr_pct/med if med>0 else 1.0
    tot=state.tick_total.get(symbol,0); wt=state.tick_walls.get(symbol,0)
    return {"vol_rel":round(vol_rel,2),"trendiness":round(trendiness(sr.get("closes",[])),3),
            "wall_share":round(wt/tot,2) if tot>0 else 0.0,"atr_pct":round(atr_pct,5)}

def recommend(reg):
    if reg["trendiness"]>0.25 and reg["vol_rel"]>1.2: return "TREND","трендовость+волатильность"
    if reg["wall_share"]>0.5 and 0.7<=reg["vol_rel"]<=1.5 and reg["trendiness"]<=0.25: return "WALL","боковик со стенами"
    if reg["vol_rel"]<0.7 and reg["trendiness"]<=0.2: return "SWING","низкая волатильность"
    return "WALL","базовый режим"

def breakout_state(kl, max_range_pct):
    if not kl or len(kl)<30: return {"active":False,"side":None,"range_pct":0,"vol_ratio":0}
    comp=kl[-25:-1]
    hi=max(float(k[2]) for k in comp); lo=min(float(k[3]) for k in comp)
    range_pct=(hi-lo)/lo if lo else 0
    vols=[float(k[5]) for k in comp]; avgv=sum(vols)/len(vols) if vols else 0
    last=kl[-2]; vol_ratio=float(last[5])/avgv if avgv else 0
    close=float(last[4]); side=None
    if range_pct<=max_range_pct:
        if close>hi: side="Buy"
        elif close<lo: side="Sell"
    return {"active":side is not None,"side":side,"range_pct":round(range_pct,4),
            "vol_ratio":round(vol_ratio,2),"high":round(hi,6),"low":round(lo,6)}

# rows: 0 pnl,1 gross,2 reason,3 side,4 exit_ts,5 ts,6 mfe,7 mae,8 strategy
def get_rows(db, symbol, hours=72):
    cur=db.cursor(); t0=time.time()-hours*3600
    cur.execute("""SELECT pnl,gross_pnl,exit_reason,side,exit_timestamp,timestamp,mfe,mae,strategy
        FROM trades WHERE symbol=? AND timestamp>=?""",(symbol,t0))
    return cur.fetchall()

def trade_metrics(rows):
    n=len(rows)
    if n==0: return {"n":0}
    wins=[r for r in rows if r[0]>0]
    gross=sum(r[1] for r in rows); fees=sum(r[1]-r[0] for r in rows); net=sum(r[0] for r in rows)
    losses=[r for r in rows if r[0]<=0]
    inst=[r for r in losses if (r[4]-r[5])<30]
    bg=sum(r[1] for r in rows if r[3]=="Buy"); sg=sum(r[1] for r in rows if r[3]=="Sell")
    mfe=[r[6] for r in rows if r[6] is not None]; mae=[r[7] for r in rows if r[7] is not None]
    be=sum(1 for r in rows if r[1]>0 and r[0]<=0)
    return {"n":n,"wr":round(len(wins)/n*100,1),"gross":round(gross,3),"fees":round(fees,3),"net":round(net,3),
            "inst_stop":round(len(inst)/len(losses),2) if losses else 0.0,
            "buy_gross":round(bg,3),"sell_gross":round(sg,3),"breakeven":be,
            "avg_mfe":round(sum(mfe)/len(mfe),4) if mfe else 0.0,
            "avg_mae":round(sum(mae)/len(mae),4) if mae else 0.0}

def halves(rows):
    if len(rows)<8: return None,None
    rows=sorted(rows,key=lambda r:r[5]); mid=len(rows)//2
    return trade_metrics(rows[:mid]), trade_metrics(rows[mid:])

def perf_by_strategy(rows):
    d={}
    for r in rows: d.setdefault(r[8] or "?",[]).append(r)
    return {s:trade_metrics(rs) for s,rs in d.items()}

def strategy_scores(rows, min_n=3):
    out={}
    for s,pm in perf_by_strategy(rows).items():
        if pm["n"]==0: continue
        out[s]={"n":pm["n"],"net":pm["net"],"net_per_trade":round(pm["net"]/pm["n"],4)}
    return out

def hour_stats(rows, worst=3):
    d={}
    for r in rows:
        h=datetime.fromtimestamp(r[5]).hour
        a=d.setdefault(h,[0,0]); a[0]+=r[0]; a[1]+=1
    lst=[{"h":h,"net":round(v[0],2),"n":v[1]} for h,v in d.items()]
    lst.sort(key=lambda x:x["net"])
    return lst[:worst]

def decide_strategy(scores, reg, bo, current, stale, min_n=3, off_floor=-0.03):
    if bo.get("active"): return "BREAKOUT",1.0,"активный пробой диапазона"
    elig={s:m for s,m in scores.items() if m["n"]>=min_n}
    prof={s:m for s,m in elig.items() if m["net_per_trade"]>0}
    if prof:
        s,b=max(prof.items(),key=lambda kv:kv[1]["net_per_trade"])
        return s,(1.0 if b["n"]>=10 else 0.5),f"лучший net/сделку {b['net_per_trade']:+.3f} (n={b['n']})"
    if elig:
        s,b=max(elig.items(),key=lambda kv:kv[1]["net_per_trade"])
        if b["net_per_trade"]>=off_floor:
            return s,0.25,f"пограничный net/сделку {b['net_per_trade']:+.3f} — канарейка ×0.25"
        if current=="OFF" and stale: return recommend(reg)[0],0.25,"ре-разведка после OFF"
        return "OFF",0.0,f"все стратегии убыточны (лучший {b['net_per_trade']:+.3f})"
    return recommend(reg)[0],0.5,"мало данных: режимная канарейка ×0.5"

def adaptive_rules(m, h1, h2, get, symbol):
    out=[]
    if m.get("n",0)<get(symbol,"min_sample"): return out
    both=lambda f:(h1 is not None and h2 is not None and f(h1) and f(h2)) or (h1 is None and f(m))
    if both(lambda x:x.get("inst_stop",0)>0.35):
        out.append(("wall_min_age_seconds",min(120,get(symbol,"wall_min_age_seconds")+15),"мгновенные стопы>35% (обе половины)"))
        out.append(("min_sl_distance_pct",min(0.02,get(symbol,"min_sl_distance_pct")*1.15),"мгновенные стопы>35%"))
    if m.get("avg_mfe",0)>0.008:
        out.append(("trail_atr_mult",min(6.0,get(symbol,"trail_atr_mult")*1.15),"MFE высокий — дать прибыли дышать"))
    if m.get("avg_mae",0)>-0.002 and m.get("inst_stop",0)>0.3:
        out.append(("min_sl_distance_pct",min(0.02,get(symbol,"min_sl_distance_pct")*1.15),"MAE мал — стопы слишком близко"))
    return out[:2]