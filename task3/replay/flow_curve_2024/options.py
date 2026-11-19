"""ATM option demand and contemporaneous term structure, with paired exit rules."""
from array import array
from bisect import bisect_right
from dataclasses import replace
import functools
import gzip
import itertools
import json
import math
import statistics
import sys
from common import *
from archive import TAPE,windows
from engine import choose_pair
from market import greeks, mark_iv, iso
from bots.straddle_grid import configure
from bots.grid import passive_order
from bots.delta_hedge import next_target
from backtest import Perpetual

ENTRIES=('upward','steep','buy_pressure','sell_pressure','upward_and_buy','upward_and_sell')
EXITS=('expiry','first_liquid','vol_recovery','pressure_reversal')

@functools.lru_cache(maxsize=160)
def window(t):
    with gzip.open(TAPE/f'{t}.json.gz','rt') as f:
        return json.load(f)

def leg_iv(row,expiry):
    n,t,mark,s,p,side,q,ident=row
    return mark_iv([t,mark*s,s,p*s,side,expiry,0,ident],float(n.split('-')[2]),n.endswith('-C'))

@functools.lru_cache(maxsize=None)
def imbalance(t,spot):
    amounts=[[0.,0.,0],[0.,0.,0]]
    for name,ts,_,_,_,side,q,_ in window(t)['pre']:
        assert t-2*HOUR<=ts<t
        if abs(float(name.split('-')[2])/spot-1)>.03:continue
        a=amounts[0 if name.endswith('-C') else 1]
        a[0]+=q if side=='buy' else -q;a[1]+=q;a[2]+=1
    return tuple(a[0]/a[1] if a[2]>=3 and a[1] else None for a in amounts)

def both_pressure(values,sign):
    return all(v is not None and sign*v>1/3 for v in values)

def bids(e,market_info,cfg):
    time,spot,ivs=market_info
    tau=(e['expiry']-time)/YEAR
    px=[]
    for j,vol in enumerate(ivs):
        mid=greeks(spot,e['strike'],tau,vol,j==0)[0]
        raw=max(0.,mid-max(.00005*spot,cfg.option_half_spread*mid))/spot
        tick=.0001 if raw<=.005 else .0005
        px.append(math.floor(raw/tick+1e-12)*tick*spot)
    return px,sum(min(.0003*spot,.125*p) for p in px)

class Options:
    def __init__(self):
        self.market=run_market()
        self.times=[t for t,_ in windows()]

    @functools.lru_cache(maxsize=None)
    def entry(self,t):
        d=window(t);x=d['expiry'];s=self.market.funding[t][0]
        pair=choose_pair(d['pre'],x,t,s)
        if not pair:return None
        k,names,iv=pair;parts=[greeks(s,k,(x-t-MINUTE)/YEAR,v,j==0) for j,v in enumerate(iv)]
        by_exp={}
        for r in d['far']:
            maturity=d['expiries'][r[0].split('-')[1]];by_exp.setdefault(maturity,[]).append(r)
        back=None
        for xx in sorted(by_exp,key=lambda z:(abs(z-t-28*DAY),z)):
            p=choose_pair(by_exp[xx],xx,t,s)
            if p:
                back=statistics.mean(p[2]);break
        iv_mid=statistics.mean(iv);pressure=imbalance(t,s)
        e=dict(scheduled=t,t=t+MINUTE,expiry=x,underlying=s,strike=k,names=names,iv=iv,front=iv_mid,back=back,pressure=pressure,f0=self.market.bars[t][3],
               mid=sum(v[0] for v in parts),delta=sum(v[1] for v in parts),gamma=sum(v[2] for v in parts))
        fixed=configure(e,CONFIG,CAPITAL)
        if not fixed:return None
        spread=back-fixed['effective_iv'] if back is not None else None
        up=spread is not None and spread>0;buy=both_pressure(pressure,1);sell=both_pressure(pressure,-1)
        e['flags']={'always':True,'upward':up,'steep':spread is not None and spread>.05,'buy_pressure':buy,'sell_pressure':sell,'upward_and_buy':up and buy,'upward_and_sell':up and sell}
        e['slope']=spread
        return e

    @functools.lru_cache(maxsize=None)
    def exit_marks(self,entry_time,t):
        e=self.entry(entry_time);matched={}
        for r in window(t)['pre']:
            if r[1]>=t-HOUR//2 and r[0] in e['names']:matched[r[0]]=r
        if len(matched)<2:return None
        ivs=[leg_iv(matched[n],e['expiry']) for n in e['names']]
        if any(v is None for v in ivs):return None
        return t+MINUTE,self.market.funding[t][0],ivs

    @functools.lru_cache(maxsize=None)
    def exit_plan(self,entry_time,kind):
        e=self.entry(entry_time)
        if kind!='expiry':
            start=bisect_right(self.times,entry_time)
            for t in self.times[start:]:
                if t>=e['expiry']:break
                mark=self.exit_marks(entry_time,t)
                if mark is None:continue
                if kind=='first_liquid':return t,mark
                if kind=='vol_recovery' and statistics.mean(mark[2])>=max(e['front']+.05,e['back'] or 0):return t,mark
                if kind=='pressure_reversal':
                    vals=e['pressure'];direction=sum(v for v in vals if v is not None)
                    if direction and both_pressure(imbalance(t,mark[1]),-math.copysign(1,direction)):return t,mark
        return e['expiry'],None

    @functools.lru_cache(maxsize=None)
    def delta_path(self,t):
        e=self.entry(t);m=self.market;basis=e['f0']-e['underlying'];out=array('d');k=e['strike'];x=e['expiry'];vc,vp=e['iv'];r2=math.sqrt(2)
        for u in range(t+MINUTE,x,MINUTE):
            if u in m.funding:basis=m.bars[u-MINUTE][3]-m.funding[u][0]
            s=m.bars[u][3]-basis;tau=(x-u-MINUTE)/YEAR
            if tau<=0:out.append(0.);continue
            root=math.sqrt(tau);log=math.log(s/k)
            out.append(.5*(math.erf((log/vc/root+.5*vc*root)/r2)+math.erf((log/vp/root+.5*vp*root)/r2)))
        return out

    def lock_for(self,t,kind):
        return None

    @functools.lru_cache(maxsize=35000)
    def overlay(self,book,t,stop,qty,grid_size,step,wings,center,cfg,lock=None):
        e=self.entry(t);m=self.market;b=Perpetual();prev=e['f0'];k=e['strike'];level=center;active=True;pending=None;days={}
        early=stop<e['expiry'];closing=stop if early else stop-30*MINUTE;u=t+MINUTE
        dp=self.delta_path(t) if book=='delta' else None
        while u<stop or abs(b.position)>1e-9:
            opening,high,low,close,vol=m.bars[u];nxt=u+MINUTE
            lock_active=lock is not None and u>=lock[0]
            if lock_active:
                pending=lock[1]*qty if u<stop else 0.
            elif u>=closing:pending=0.
            if vol>0 and pending is not None and (not lock_active or vol>=abs(pending-b.position)):
                if abs(pending-b.position)>1e-9:b.order(pending-b.position,opening,prev,cfg.taker_bp,cfg.slippage_bp)
                pending=None
            b.carry_minute(prev,close,m.funding.get(nxt),cfg)
            if book=='combo' and u<closing and active and not lock_active:
                dq,price,_=passive_order(k,level,step,wings,grid_size,prev,high,low,vol,cfg.trade_through_bp,b.position,0)
                if dq:
                    b.order(dq,price,close,cfg.maker_bp);level+=-1 if dq>0 else 1
                if abs(close-k)>wings*step:active=False;pending=0.
            if book=='delta' and nxt<closing and not lock_active:
                pending=next_target(dp[(u-t-MINUTE)//MINUTE],qty,b.position,.30,.50,cfg.perp_lot)
            if nxt%DAY==0:days[nxt]=b.pnl()
            prev=close;u=nxt
        return b.values(),b.fills,u,days

    def trade(self,book,e,exit_kind,eq,stress):
        cfg=STRESS if stress else CONFIG
        p=configure(e,cfg,eq)
        if p is None:return None
        t=e['scheduled'];x=e['expiry'];qty=p['units'];chosen,mark=self.exit_plan(t,exit_kind)
        stop=mark[0] if mark else x
        if mark:
            sell,exit_fee=bids(e,mark,cfg);gross=sum(sell);mode='early'
        else:
            settle=self.market.delivery[datestr(x)];gross=abs(settle-e['strike']);exit_fee=min(.00015*settle,.125*gross);mode='expiry'
        initial=p['premium']+p['option_fee'];carry=initial*cfg.carry*(stop-e['t'])/YEAR
        parts={'option_mid_to_exit':qty*(gross-e['mid']),'entry_spread':-qty*(p['premium']-e['mid']),'entry_fee':-qty*p['option_fee'],'exit_fee':-qty*exit_fee,
               'conversion':-qty*cfg.fx_rate*(initial+gross-exit_fee),'option_carry':-qty*carry}
        lock=self.lock_for(t,exit_kind)
        if book=='plain':funds,filled,closed,marks={},0,stop,{}
        else:
            funds,filled,closed,marks=self.overlay(book,t,stop,qty,p['quantity'],p['step'],p['wings'],p['center'],cfg,lock)
        parts.update(funds);pnl=sum(parts.values());valuations=[]
        for u in range((t//DAY+1)*DAY,closed+1,DAY):
            if u>=stop:opt=sum(v for k,v in parts.items() if k not in funds)
            else:
                s=self.market.funding[u][0];gross_mark=sum(greeks(s,e['strike'],(x-u)/YEAR,v,j==0)[0] for j,v in enumerate(e['iv']))
                opt=qty*(gross_mark-initial*(1+cfg.fx_rate+cfg.carry*(u-e['t'])/YEAR))
            valuations.append([u,marks.get(u,0.)+opt])
        return dict(opened=e['t'],closed=closed,label=datestr(t),before=eq,after=eq+pnl,pnl=pnl,q=qty,perp_fills=filled,parts=parts,marks=valuations,mode=mode,
                    option_exit=stop,expiry=x,legs=e['names'],slope=e['slope'],pressure=e['pressure'],lock=lock)

    def run(self,book,rule,lo,hi,skip=frozenset(),stress=False):
        left=timestamp(lo);right=timestamp(hi);until=left;rows=[];eq=CAPITAL
        for t in self.times:
            if t<until or t>=right or t%DAY!=12*HOUR:continue
            e=self.entry(t)
            if not e or e['expiry']>right or not e['flags'][rule['entry']]:continue
            until=e['expiry']  # one planned campaign per weekly expiry even after an omitted cycle
            if e['t'] in skip:continue
            row=self.trade(book,e,rule['exit'],eq,stress)
            if row:
                rows.append(row);eq=row['after'];until=max(until,row['closed'])
        return rows

    def train(self,book):
        scores=[]
        for entry,exit_kind in itertools.product(ENTRIES,EXITS):
            rule=dict(entry=entry,exit=exit_kind);rows=self.run(book,rule,START,CUT);s=summary(rows,START,CUT)
            counts=[sum(r['label'].startswith(y) for r in rows) for y in ('2022','2023')]
            scores.append(dict(rule=rule,eligible=s['n']>=12 and min(counts)>=3,**s))
        select(book,scores)

if __name__=='__main__':
    model=Options();book=sys.argv[1]
    if 'train' in sys.argv:model.train(book)
    else:detailed(model.run,book,read_result(book+'-frozen.json')['rule'])
