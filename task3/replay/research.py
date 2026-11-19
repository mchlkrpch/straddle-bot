"""Offline BTC straddle/grid experiment. Python 3.10+, standard library."""
import argparse, csv, json, math, statistics
from datetime import date
from pathlib import Path
from dataclasses import replace
from backtest import Market, Settings, BOOKS, COSTS, replay, implied_straddle
from market import ROOT, MINUTE, DAY, HOUR, YEAR, timestamp, iso, greeks, moments, read

START, SPLIT, END = '2022-05-01', '2025-01-01', '2026-11-14'
ANCHOR = timestamp('2022-05-06T12:00:00')
BASE = Settings(breakout='full', taker_bp=5, fx_rate=.001)
CAPITAL = 150000.

from bots.straddle_grid import configure, floor_lot, signal

def option_net(m,e,cfg):
    settle=m.delivery[iso(e['expiry'])[:10]]
    intrinsic=abs(settle-e['strike'])
    delivery=min(settle*.00015,intrinsic*.125)
    paid=e['premium']+e['option_fee']
    carry=paid*cfg.carry*(e['expiry']-e['t'])/YEAR
    fx=cfg.fx_rate*(paid+intrinsic-delivery)
    return e['units']*(intrinsic-delivery-paid-carry-fx)

def study(m,start,end,cfg=BASE,mode='original',gate=1.,detail=False):
    equity=CAPITAL; completed=[]; last=0
    state={b:dict(start=CAPITAL,peak=CAPITAL,dd=0.) for b in BOOKS}
    for t in moments(start,end):
        phase=((t-ANCHOR)//(7*DAY))%2
        if (mode=='original' and phase) or (mode=='offset' and not phase) or t<last:continue
        if t//DAY*DAY+cfg.days*DAY+8*HOUR>timestamp(end):continue
        o,_=m.entry(t,cfg)
        if not o:continue
        past=[m.funding[t-i*DAY][0] for i in range(8)]
        # Published signal: the unrounded model premium from the six-hour observation window.
        score=signal(past,o['premium'])
        if score<gate:continue
        e=configure(o,cfg,equity)
        if not e:continue
        row=replay(m,e,cfg,trace='hourly' if detail else 'none',benchmark=detail,
                   with_option=detail,account=state if detail else None)
        opt=option_net(m,e,cfg)
        grid=row['components']['grid']['net']
        net=opt+grid
        if detail:
            assert abs(net-row['components']['combo']['net'])<1e-5
            assert abs(row['path'][-1][5]-net)<1e-5
            for b in BOOKS:
                state[b]['start']+=row['components'][b]['net']
        row.update(score=score,equity_before=equity,return_on_nav=net/equity,net=net)
        equity+=net
        assert equity>0
        completed.append(row);last=e['expiry']
    dur=(date.fromisoformat(end)-date.fromisoformat(start)).days
    return {'start':start,'end':end,'equity':equity,'cycles':len(completed),
            'cagr':(equity/CAPITAL)**(365/dur)-1,
            'return':equity/CAPITAL-1,'dd':{b:s['dd'] for b,s in state.items()} if detail else {}},completed

def daily(rows,start,end):
    changes={}
    for r in rows:
        prev=[0.]*4
        for t,*v in r['capital_path']:
            day=(t+DAY-1)//DAY*DAY
            inc=changes.setdefault(day,[0.]*4)
            for j in range(4):
                inc[j]+=v[j]-prev[j]
            prev=v
    eq=[CAPITAL]*4
    for t in range(timestamp(start),timestamp(end)+1,DAY):
        eq=[x+y for x,y in zip(eq,changes.get(t,[0.]*4))]
        yield [iso(t)[:10],*eq]

def stats(rows,result):
    ds=list(daily(rows,result['start'],result['end']))
    out={}
    for j,b in enumerate(BOOKS,1):
        changes=[v[j]/u[j]-1 for u,v in zip(ds,ds[1:])]
        profits=[r['components'][b]['net'] for r in rows]
        sigma=statistics.stdev(changes) if len(changes)>1 else 0
        out[b]=dict(pnl=sum(profits),cagr=(ds[-1][j]/CAPITAL)**(365/(len(ds)-1))-1,dd=result['dd'][b],
            sharpe=statistics.mean(changes)*math.sqrt(365)/sigma if sigma else 0,
            wins=sum(x>0 for x in profits),worst5mean=statistics.mean(sorted(profits)[:math.ceil(.05*len(profits))]) if profits else 0)
    return out

def run_market():
    m=Market(ROOT/'data',START,END)
    m.delivery.update(read(ROOT/'data/exact-btc-usd.json.gz'))
    return m

def compact(r):
    return {k:r[k] for k in ('entry','components','path','boundary','rv','regime','score','return_on_nav','net','equity_before','paired_grid','inventory_grid','fills','grid_margin')}

def write_csv(file,rows):
    with file.open('w',newline='') as f:csv.writer(f).writerows(rows)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',default=START);parser.add_argument('--end',default=END)
    parser.add_argument('--step-bp',type=float,default=50);parser.add_argument('--size',type=float,default=.5)
    parser.add_argument('--width',type=float,default=1);parser.add_argument('--days',type=int,choices=(7,14,21,28),default=14)
    parser.add_argument('--gate',type=float,default=1.);parser.add_argument('--mode',choices=('original','offset','weekly'),default='original')
    parser.add_argument('--save',action='store_true');a=parser.parse_args()
    if not START<=a.start<a.end<=END or a.days!=14 and a.start<'2025-08-01':parser.error('Invalid date range for archived marks.')
    if not (a.step_bp>0 and a.size>=0 and a.width>0 and a.gate>=0):parser.error('Invalid trading parameters.')
    m=run_market();cfg=replace(BASE,step_bp=a.step_bp,size=a.size,width=a.width,days=a.days)
    result,rows=study(m,a.start,a.end,cfg,a.mode,a.gate,True)
    result['books']=stats(rows,result)
    if a.save:
        folder=ROOT/'results';folder.mkdir(exist_ok=True)
        (folder/'run.json').write_text(json.dumps(dict(result=result,cycles=[compact(r) for r in rows]),ensure_ascii=False))
        write_csv(folder/'equity.csv',[['date_utc',*BOOKS],*daily(rows,a.start,a.end)])
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
