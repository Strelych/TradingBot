import analyzer

def run():
    # test_recommend_trend
    reg={"atr_pct":0.002,"trendiness":0.2,"vol_rel":1.0,"wall_share":0.0}
    s,r=analyzer.recommend(reg)
    assert s=="TREND"

    # test_recommend_wall
    reg={"atr_pct":0.002,"trendiness":0.1,"vol_rel":1.0,"wall_share":0.6}
    s,r=analyzer.recommend(reg)
    assert s=="WALL"

    # test_recommend_swing
    reg={"atr_pct":0.002,"trendiness":0.05,"vol_rel":0.6,"wall_share":0.0}
    s,r=analyzer.recommend(reg)
    assert s=="SWING"

    # test_recommend_fee_negative
    reg={"atr_pct":0.0005,"trendiness":0.3,"vol_rel":1.5,"wall_share":0.0}
    s,r=analyzer.recommend(reg)
    assert s=="FEE_NEGATIVE"

    # test_decide_cold_start
    scores={}
    reg={"atr_pct":0.002,"trendiness":0.2,"vol_rel":1.0,"wall_share":0.0}
    bo={"active":False}
    m={"n":0}
    strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current='AUTO',stale=False,m=m)
    assert rm==0.5

    # test_decide_off_split
    scores={"TREND":{"n":5,'net_per_trade':-0.05}}
    reg={"atr_pct":0.002}
    bo={"active":False}
    m={"n":30}
    h1={'net_per_trade':-0.04}
    h2={'net_per_trade':-0.05}
    strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current='TREND',stale=False,m=m,h1=h1,h2=h2)
    assert strat=='OFF' and rm==0.0

    # test_decide_profitable
    scores={"TREND":{"n":12,'net_per_trade':0.02},"WALL":{"n":6,'net_per_trade':0.01}}
    reg={"atr_pct":0.002}
    bo={"active":False}
    m={"n":30}
    strat,rm,reason=analyzer.decide_strategy(scores,reg,bo,current='AUTO',stale=False,m=m)
    assert strat=='TREND' and rm==1.0

    print('ALL CHECKS OK')

if __name__=='__main__':
    run()
