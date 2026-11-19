"""Run an integrated account holding the frozen trend and a funded native-straddle grid."""
from bisect import bisect_right
from dataclasses import replace
import csv
import json
from pathlib import Path
import sys
from unittest.mock import patch

from study import HERE as EXECUTION
HERE=EXECUTION.parent/'calibration'
sys.path.insert(0,str(HERE.parent/'execution_4pct'))
from packages import Packages
from study import CAPITAL, START, CUT, END, DAY, datestr, timestamp
import trend_check
from trend_check import bot, ORIG, campaigns

def option_values(rows,times,capital):
    ends=[r['closed'] for r in rows]
    marks=[dict(r['marks']) for r in rows]
    values=[]
    for t in times:
        i=bisect_right(ends,t)
        if i<len(rows) and rows[i]['opened']<t<rows[i]['closed']:
            values.append(rows[i]['before']+marks[i][t])
        else:
            values.append(rows[i-1]['after'] if i else capital)
    return values

def stats(nav,times):
    previous=nav[0]
    annual={}
    peak=previous
    dd=0.
    for t,v in zip(times[1:],nav[1:]):
        peak=max(v,peak)
        dd=max(dd,1-v/peak)
        if datestr(t).endswith('01-01') or t==times[-1]:
            annual[datestr(t-1)[:4]]=v/previous-1
            previous=v
    return dict(equity=nav[-1],cagr=(nav[-1]/nav[0])**(365*DAY/(times[-1]-times[0]))-1,
                dd_daily=dd,annual=annual)

def simulate(sim,lo,hi):
    half=CAPITAL/2
    times=list(range(timestamp(lo),timestamp(hi)+1,DAY))
    rows,_=sim.run_packages('combo',lo,hi,initial_capital=half)
    option=option_values(rows,times,half)
    with patch.object(bot,'CAPITAL',half):
        tr=bot.run(trend_check.market,lo,hi,params,include_trail=True)
        trend=[half]+[v['equity'] for v in tr['daily']]
        groups=campaigns(tr)
        top=max([(g['pnl'],'trend',g['start'],g['stop']) for g in groups]+
                [(r['pnl'],'options',r['opened'],r['closed']) for r in rows])
        exopt,extrend=option,trend
        if top[1]=='trend':
            original=bot.make_signal
            def skip(m,t,p,*args):
                s=original(m,t,p,*args)
                return replace(s,weight=0.) if top[2]<=t<top[3] else s
            with patch.object(bot,'make_signal',skip):
                ex=bot.run(trend_check.market,lo,hi,params,include_trail=True)
                extrend=[half]+[v['equity'] for v in ex['daily']]
        else:
            ex,_=sim.run_packages('combo',lo,hi,omit=top[2],initial_capital=half)
            exopt=option_values(ex,times,half)
    nav=[a+b for a,b in zip(trend,option)]
    extra=stats([a+b for a,b in zip(extrend,exopt)],times)
    m=stats(nav,times)
    m.update(ex_best=extra['cagr'],annual_ex_best=extra['annual'],best_component=top,
             option_end=option[-1],trend_end=trend[-1],option_cycles=len(rows))
    return m,times,nav

if __name__=='__main__':
    params=bot.Parameters(**json.loads((ORIG/'selection.json').read_text())['winner']['params'])
    trend_check.market=bot.Market(ORIG/'deribit_5m.json.gz')
    sim=Packages(continuous=True)
    out={}
    for label,lo in [('full',START),('recent',CUT)]:
        out[label],times,hybrid=simulate(sim,lo,END)
        print(label,out[label],flush=True)
    (HERE/'combined_results.json').write_text(json.dumps(out,indent=1))
    standalone=bot.run(trend_check.market,CUT,END,params,include_trail=True)
    trend=[CAPITAL]+[r['equity'] for r in standalone['daily']]
    options=json.loads((HERE/'option_results.json').read_text())['books']
    grid=json.loads((HERE/'grid_walk.json').read_text())['cycles']
    lines=[option_values(options['combo']['cycles'],times,CAPITAL),option_values(grid,times,CAPITAL),
           option_values(options['delta']['cycles'],times,CAPITAL),trend,hybrid]
    with (HERE/'comparison_daily.csv').open('w',newline='') as f:
        w=csv.writer(f)
        w.writerow(['date_utc','straddle_grid','grid','straddle_delta','trend_42d','trend_straddle_50_50'])
        w.writerows(zip(map(datestr,times),*lines))
