"""Weekday option and short-horizon grid experiments. Imports the V1 execution engine."""
import datetime as dt
import functools
import gzip
import json
import math
from pathlib import Path
import statistics
import sys
from dataclasses import replace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
from research import BASE, CAPITAL, START, END, configure, run_market, option_net
from market import DAY,HOUR,MINUTE,YEAR,greeks,mark_iv,iso,timestamp
from backtest import replay
from bots.grid import choppy_week
from grid_replay import simulate as standalone_grid

CUT='2024-01-01'
CONFIG=replace(BASE,perp_tick=1.,perp_lot=.001)
STRESS=replace(CONFIG,option_half_spread=.05,taker_bp=5.,slippage_bp=5.,trade_through_bp=5.,fx_rate=.002)
RESULTS=ROOT/'results'
RESULTS.mkdir(exist_ok=True)
def read(path):
    with gzip.open(path,'rt') as f:return json.load(f)
def datestr(t):return iso(t)[:10]

def choose_pair(rows,expiry,t,spot):
    groups={}
    for name,stamp,mark,index,price,direction,amount,tid in rows:
        if stamp>=t or stamp<t-2*HOUR:continue
        _,_,strike,side=name.split('-');strike=float(strike)
        if abs(strike/spot-1)>.03:continue
        groups.setdefault(strike,{})[side]=(name,[stamp,mark*index,index,price*index,direction,expiry,0,tid])
    for k in sorted(groups,key=lambda x:(abs(x-spot),x)):
        if not all(x in groups[k] for x in ('C','P')):continue
        names,obs=zip(*(groups[k][i] for i in ('C','P')))
        iv=[mark_iv(row,k,j==0) for j,row in enumerate(obs)]
        if all(x is not None for x in iv):return k,list(names),iv
    return None

class Weekdays:
    def __init__(self):
        self.market=run_market()
        # Entry IV is the delta policy's frozen parameter. Official delivery sets terminal cash flows.
        self.market.option_marks=lambda e,stop: [iter(()),iter(())]
        self.sources={d['scheduled']:d for f in sorted((ROOT/'observations').glob('*.gz')) for d in [read(f)]}
        self._grid_rows={}
    @functools.lru_cache(maxsize=None)
    def friday(self,t,cfg):
        return self.market.entry(t,cfg)[0]
    @functools.lru_cache(maxsize=None)
    def get(self,t,cfg):
        m=self.market;obj=self.sources[t];expiry=obj['expiry']
        s=m.funding[t][0];pair=choose_pair(obj['pre'],expiry,t,s)
        if not pair:return None
        k,names,iv=pair;e=t+MINUTE
        tau=(expiry-e)/YEAR
        legs=[greeks(s,k,tau,v,j==0) for j,v in enumerate(iv)]
        mid=sum(x[0] for x in legs)
        grid_step=round(s*cfg.step_bp/10000/cfg.perp_tick)*cfg.perp_tick
        f0=m.bars[t][3]
        entry=dict(scheduled=t,t=e,expiry=expiry,underlying=s,strike=k,names=names,iv=iv,
                   mid=mid,delta=sum(x[1] for x in legs),gamma=sum(x[2] for x in legs),
                   step=grid_step,f0=f0,center=round((f0-k)/grid_step))
        fixed=configure(entry,cfg,CAPITAL)
        if not fixed:return None
        premium=fixed['premium']+fixed['option_fee']
        # Matched historical weekday-to-Friday displacements; suppress the largest single outlier.
        ratios=[]
        for j in range(1,13):
            past=t-7*j*DAY;ex=expiry-7*j*DAY;settle=m.delivery.get(datestr(ex))
            if ex>=t or past not in m.funding or settle is None:continue
            ratios.append(abs(s*settle/m.funding[past][0]-k))
        ranges=[m.funding[t-j*DAY][0] for j in range(8)]
        minute_prices=[m.bars[u-MINUTE][3] for u in range(t-5*DAY,t+1,15*MINUTE)]
        rv5=math.sqrt(sum(math.log(b/a)**2 for a,b in zip(minute_prices,minute_prices[1:]))*365/5)
        weekday=dt.date.fromisoformat(obj['date']).weekday()
        last_friday=t-((weekday-4)%7)*DAY
        prev=self.friday(last_friday,replace(cfg,days=7)) if last_friday>=timestamp(START) else None
        hist=(sum(ratios)-max(ratios))/(len(ratios)-1) if len(ratios)>=6 else 0
        front=sum(iv)/sum(prev['iv']) if prev else None
        entry['flags']={'always':True,'weekday_history':hist>premium,'weekday_history_1.2':hist>1.2*premium,
                        'rv5':rv5>fixed['effective_iv'],'front_crush':front is not None and front<.90,
                        'front_rich':front is not None and front>1.10,
                        'range':(max(ranges)-min(ranges))*math.sqrt((expiry-e)/(7*DAY))>=premium}
        entry['weekday']=weekday
        return entry
    def candidate_entries(self,lo,hi,cfg,weekday,flag):
        until=0
        for t in sorted(self.sources):
            if t<timestamp(lo) or t>=timestamp(hi) or t<until:continue
            obj=self.sources[t]
            if obj['expiry']>timestamp(hi):continue
            day=dt.date.fromisoformat(obj['date']).weekday()
            if weekday!=7 and day!=weekday:continue
            o=self.get(t,cfg)
            if o and o['flags'][flag]:
                yield o
                until=o['expiry']

def result(cycles,lo,hi):
    eq=CAPITAL if not cycles else cycles[-1]['after']
    years=(timestamp(hi)-timestamp(lo))/YEAR
    cagr=lambda v:(v/CAPITAL)**(1/years)-1
    ordered=sorted(cycles,key=lambda r:r['r'],reverse=True)
    def without(n):
        removed={r['date'] for r in ordered[:n]}
        return math.expm1(sum(math.log1p(r['r']) for r in cycles if r['date'] not in removed)/years)
    return dict(equity=eq,n=len(cycles),cagr=cagr(eq),without_best=without(1),without_three=without(3),
                worst=min((r['r'] for r in cycles),default=0),wins=sum(r['pnl']>0 for r in cycles))

BOOKCOL={'combo':1,'grid':3,'delta':4}
class Run(Weekdays):
    def __init__(self):
        super().__init__()
        self.cache={}
    def cycle(self,book,base,cfg,eq,rule=None):
        e=configure(base,cfg,eq)
        if not e:return None
        key=(book,e['t'],e['expiry'],e['units'],e['quantity'],cfg)
        if key not in self.cache:
            r=replay(self.market,e,cfg,trace='none',benchmark=book=='delta',with_option=book!='grid')
            comp=r['components'][book]
            if book!='grid':
                assert math.isclose(r['components']['straddle']['net'],option_net(self.market,e,cfg),abs_tol=1e-6)
            self.cache[key]=dict(pnl=comp['net'],parts=comp,fills=r['hedge_fills'] if book=='delta' else r['fills'],
                                 daily=[(v[0],v[BOOKCOL[book]]) for v in r['capital_path']])
        return e,self.cache[key]
    def grid_entries(self,lo,hi,cfg,rule):
        from market import moments
        for t in moments(lo,hi):
            end=t//DAY*DAY+rule['horizon']*DAY+8*HOUR
            if end>timestamp(hi):continue
            o=self.friday(t,replace(cfg,days=7))
            if not o:continue
            ds=[self.market.funding[t-i*DAY][0] for i in range(8)]
            if rule['flag']=='chop' and not choppy_week(ds):continue
            if rule['flag']=='quiet' and o['rv7']>=.85*o['effective_iv']:continue
            # Range, gamma and order sizes use the original 7-day pair as reference.
            # Change only the simulated close time for the standalone perpetual book.
            yield o,end
    def portfolio(self,book,rule,lo,hi,stress=False,skip=frozenset()):
        cfg=replace(STRESS if stress else CONFIG,hedge_band_btc=.30,hedge_fraction=.5)
        eq=CAPITAL;rows=[];nav={}
        if book=='grid':
            cfg=replace(cfg,breakout=rule['close'],inventory_levels=rule['cap'])
            entries=self.grid_entries(lo,hi,cfg,rule)
        else:
            entries=((o,o['expiry']) for o in self.candidate_entries(lo,hi,cfg,rule['day'],rule['flag']))
        available=0
        for o,end in entries:
            if o['t']<available:continue
            date=datestr(o['t'])
            if date in skip:continue
            if book=='grid':
                e=configure(o,cfg,eq)
                if not e:continue
                e['expiry']=end
                key=(book,e['t'],end,e['quantity'],cfg)
                if key not in self.cache:
                    self.cache[key]=standalone_grid(self.market,e,cfg,end)
                rr=self.cache[key]
            else:
                cycle=self.cycle(book,o,cfg,eq,rule)
                if not cycle:continue
                e,rr=cycle
            for t,v in rr['daily']:nav[t]=eq+v
            pnl=rr['pnl'];after=eq+pnl
            rows.append(dict(date=date,expiry=datestr(end),q=e['units'],pnl=pnl,r=pnl/eq,after=after,parts=rr['parts'],fills=rr['fills'],names=e['names']))
            eq=after
            available=rr.get('actual_close',end)
            if 'exit_sales' in rr:rows[-1]['exit_sales']=rr['exit_sales']
        from bisect import bisect_right
        ts=sorted(nav);peak=CAPITAL;dd=0.;yearly={};base=CAPITAL
        for t in range(timestamp(lo),timestamp(hi)+1,DAY):
            i=bisect_right(ts,t)-1;v=nav[ts[i]] if i>=0 else CAPITAL
            peak=max(peak,v);dd=max(dd,1-v/peak)
            if t>timestamp(lo) and (datestr(t).endswith('01-01') or t==timestamp(hi)):
                yearly[datestr(t-1)[:4]]=v/base-1;base=v
        assert math.isclose(v,eq,abs_tol=1e-5)
        summary=result(rows,lo,hi)
        summary.update(dd_daily=dd,annual=yearly)
        return summary,rows

def rules(book):
    if book=='grid':
        return [dict(horizon=h,flag=f,close=c,cap=cap) for h in (2,3,7) for f in ('always','chop','quiet')
                for c in ('full','hold') for cap in (1,0)]
    return [dict(day=w,flag=f) for w in (0,2,3,7) for f in ('always','weekday_history','weekday_history_1.2','rv5','front_crush','front_rich','range')]

def train(book):
    sim=Run();scores=[]
    for rule in rules(book):
        score,rows=sim.portfolio(book,rule,START,CUT)
        year_counts=[sum(x['date'].startswith(y) for x in rows) for y in ('2022','2023')]
        scores.append(dict(rule=rule,eligible=score['n']>=12 and min(year_counts)>=3,**score))
    scores.sort(key=lambda x:(x['eligible'], x['without_best'],x['cagr']),reverse=True)
    (RESULTS/(book+'-selection.json')).write_text(json.dumps(scores,indent=2))
    print(book,[(v['rule'],round(100*v['cagr'],2),round(100*v['without_best'],2),v['n']) for v in scores[:5]],flush=True)

if __name__=='__main__':train(sys.argv[1])
