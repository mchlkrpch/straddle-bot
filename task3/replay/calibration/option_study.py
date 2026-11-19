"""Expanding-window parameter selection shared by both long-straddle books."""
import functools
import itertools
import json
import math
from pathlib import Path
from statistics import mean
import sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'execution_4pct'))
from packages import Packages, mark_iv, option_expiry
from study import BASE, STRESS, START, END, CUT, HOUR, DAY, YEAR, floor_lot, greeks, ci, datestr, summary, timestamp

ORIGINAL='original'
RULES=[(d,m,v) for d,m,v in itertools.product((7,28),(.9,1.),(False,True))]

class Rules(Packages):
    def __init__(self):
        super().__init__(continuous=True)
        # Empty or buy-only hours cannot fill a standing bid. signal_for() is independently causal.
        self.hours=[t for t in self.hours if any(r[2]=='sell' for r in self.by_time.get(t,()))]
        self.policy={}

    @functools.lru_cache(None)
    def rv(self,t):
        data=self.market.funding
        if any(t-j*DAY not in data for j in range(15)):
            return None
        prices=[data[t-j*DAY][0] for j in range(15)]
        return math.sqrt(sum(math.log(a/b)**2 for a,b in zip(prices,prices[1:])))

    @functools.lru_cache(None)
    def choice(self,t,tenor):
        spot=self.market.funding[t][0]
        snapshots={}
        for prior in range(t-72*HOUR,t,HOUR):
            for row in self.by_time.get(prior,()):
                sym=row[1]
                old=snapshots.get(sym)
                snapshots[sym]=((old[0] if old else 0.)+row[4],row)
        choices=[]
        for sym,(volume,last) in snapshots.items():
            strike=float(sym.rsplit('-',1)[1])
            expiry=option_expiry(sym.replace('-STRD-','-')+'-C')
            if abs(strike/spot-1)<=.03 and 6*HOUR<=expiry-t<=tenor*DAY:
                choices.append((-volume,abs(strike-spot),expiry,sym,strike,last))
        for _,__,expiry,sym,k,row in sorted(choices):
            stamp,_,_,_,_,s,_,marks,_=row
            vols=[mark_iv((stamp,v*s,s,v*s,None,expiry,0,None),k,j==0) for j,v in enumerate(marks)]
            if any(v is None for v in vols):
                continue
            control=.975*sum(greeks(spot,k,(expiry-t)/YEAR,v,j==0)[0] for j,v in enumerate(vols))/spot
            plain,normalized=[],[]
            nowvol=self.rv(t)
            for j in range(1,27):
                lag=j*7*DAY
                if expiry-lag>=t or t-lag not in self.market.funding:
                    continue
                d=self.market.delivery.get(datestr(expiry-lag))
                if not d:
                    continue
                move=math.log(d/self.market.funding[t-lag][0])
                plain.append(abs(spot*math.exp(move)-k)/spot)
                pastvol=self.rv(t-lag)
                if pastvol and nowvol:
                    ratio=max(2/3,min(1.5,nowvol/pastvol))
                    normalized.append(abs(spot*math.exp(ratio*move)-k)/spot)
            def robust(obs):
                return mean(sorted(obs)[:-1]) if len(obs)>=8 else None
            return dict(spot=spot,expiry=expiry,strike=k,symbol=sym,vols=vols,
                        plain=robust(plain),normalized=robust(normalized),mark_cap=control)
        return None

    def signal(self,t):
        rule=self.policy[datestr(t)[:4]]
        if rule==ORIGINAL:
            return super().signal(t)
        tenor,mult,normalize=rule
        d=self.choice(t,tenor)
        if not d:
            return None
        estimate=d['normalized' if normalize else 'plain']
        if estimate is None:
            return None
        return dict(d,limit=floor_lot(min(mult*estimate,d['mark_cap']),.0001),
                    control=floor_lot(d['mark_cap'],.0001))

def train(sim):
    log={}
    policy={}
    for year in (2024,2025,2026):
        end=f'{year}-01-01'
        scores=[]
        for rule in [ORIGINAL]+RULES:
            sim.policy={str(y):rule for y in range(2022,year)}
            rows,_=sim.run_packages('plain',START,end)
            m=summary(rows,START,end)
            byyear={}
            for r in rows:
                byyear.setdefault(datestr(r['closed']-1)[:4],[]).append(math.log(r['after']/r['before']))
            eligible=len(rows)>=8 and len(byyear)>=2 and min(len(v) for v in byyear.values())>=2
            worst=min((math.expm1(sum(rs)-max(rs)) for rs in byyear.values()),default=-1)
            scores.append(dict(rule=rule,eligible=eligible,worst=worst,counts={k:len(v) for k,v in byyear.items()},
                               cagr=m['cagr'],rank_ex_best=m['rank_ex_best']))
        scores.sort(key=lambda r:(r['eligible'],r['worst'],r['rank_ex_best']),reverse=True)
        first=scores[0]
        choice=first['rule'] if first['eligible'] and first['worst']>0 else ORIGINAL
        policy[str(year)]=choice
        log[str(year)]=dict(scores=scores,choice=choice)
        print(end,'chosen',choice,'top',first,flush=True)
    return policy,log

def evaluate(sim,book):
    rows,_=sim.run_packages(book,CUT,END)
    best=max(rows,key=lambda r:r['pnl'],default=None)
    ex,_=sim.run_packages(book,CUT,END,omit=best['opened']) if best else (rows,0)
    stress,_=sim.run_packages(book,CUT,END,STRESS)
    m=summary(rows,CUT,END)
    excluded=summary(ex,CUT,END)
    m.update(ex_best=excluded['cagr'],annual_ex_best=excluded['annual'],stress_cagr=summary(stress,CUT,END)['cagr'],
             best=best['label'] if best else None,best_pnl=best['pnl'] if best else None,ci=ci(rows,CUT,END))
    return dict(metrics=m,cycles=rows)

if __name__=='__main__':
    sim=Rules()
    policy,log=train(sim)
    (HERE/'option_training.json').write_text(json.dumps(log,indent=1))
    sim.policy=policy
    out={b:evaluate(sim,b) for b in ('combo','delta','plain')}
    (HERE/'option_results.json').write_text(json.dumps(dict(policy=policy,books=out),separators=(',',':')))
    for k,d in out.items():
        print(k,d['metrics']['annual'],d['metrics']['cagr'],d['metrics']['ex_best'],flush=True)
