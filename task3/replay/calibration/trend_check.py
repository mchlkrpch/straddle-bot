"""Replay frozen trend strategy and remove its most profitable same-direction campaign."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
ORIG=HERE/'trend_original'
sys.path.insert(0,str(ORIG))
from core import trend as bot
from run_long import FROZEN_BOT, FROZEN_SELECTION, years

def campaigns(trail):
    groups=[]
    last=0
    for d in trail['decisions']:
        qty=d['quantity']
        side=(qty>0)-(qty<0)
        if side==last:
            continue
        # At sign reversal, separate the closing cost of the old side and opening cost of the new side.
        nav=d['equity']+abs(qty)*d['fill_price']*(market.fee+bot.SLIPPAGE_BP/10000)
        if last:
            groups[-1].update(stop=d['time'],pnl=nav-groups[-1]['before'])
        if side:
            groups.append(dict(start=d['time'],before=nav,side=side))
        last=side
    if last:
        groups[-1].update(stop=trail['daily'][-1]['time'],pnl=trail['daily'][-1]['equity']-groups[-1]['before'])
    assert math.isclose(sum(x['pnl'] for x in groups),trail['daily'][-1]['equity']-bot.CAPITAL,abs_tol=.001)
    return groups

def inspect(left,right):
    base=bot.run(market,left,right,param,include_trail=True)
    groups=campaigns(base)
    best=max(groups,key=lambda x:x['pnl'])
    def omit(m,t,p,exposure=None,buy_hold=False):
        signal=original(m,t,p,exposure,buy_hold)
        return replace(signal,weight=0.) if best['start']<=t<best['stop'] else signal
    try:
        bot.make_signal=omit
        without=bot.run(market,left,right,param,include_trail=True)
    finally:
        bot.make_signal=original
    # Match 5bp taker assumption of the three newer accounts (extra 1.5bp of turnover).
    common=bot.run(market,left,right,param,extra_bps=1.5)
    return dict(metrics=base['stats'],annual=years(base['daily']),ex_best=without['stats']['cagr'],
                annual_ex_best=years(without['daily']),best=best,groups=groups,common_fee_cagr=common['stats']['cagr'],
                end_nav=base['daily'][-1]['equity'])

if __name__=='__main__':
    for file,ref in [('bot.py',FROZEN_BOT),('selection.json',FROZEN_SELECTION)]:
        path = Path(bot.__file__) if file=='bot.py' else ORIG/file
        assert hashlib.sha256(path.read_bytes()).hexdigest()==ref
    param=bot.Parameters(**json.loads((ORIG/'selection.json').read_text())['winner']['params'])
    market=bot.Market(ORIG/'deribit_5m.json.gz')
    original=bot.make_signal
    out={label:inspect(lo,'2026-11-14') for label,lo in [('full','2022-05-01'),('recent','2024-01-01')]}
    saved=json.loads((ORIG/'published.json').read_text())['results']['full']
    for key in ['cagr','max_drawdown','pnl']:
        if key=='pnl':
            assert math.isclose(out['full']['end_nav']-bot.CAPITAL,saved[key],abs_tol=1e-6)
        else:
            assert math.isclose(out['full']['metrics'][key],saved[key],abs_tol=1e-12)
    (HERE/'trend_results.json').write_text(json.dumps(out,indent=1))
    for k,r in out.items():
        print(k,param,r['end_nav'],r['metrics']['cagr'],r['ex_best'],r['metrics']['max_drawdown'],r['common_fee_cagr'],flush=True)
