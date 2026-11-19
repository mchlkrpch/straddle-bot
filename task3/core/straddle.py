"""Native BTC-STRD package purchases. One standing combination bid per account."""
import csv
from collections import defaultdict
from bisect import bisect_left
import functools
import gzip
import json
import math
import sys
from statistics import mean
from study import (BASE, STRESS, CAPITAL, CUT, END, START, DAY, HOUR, MINUTE, YEAR, HERE,
                   Study, ci, datestr, floor_lot, greeks, option_fee, summary, timestamp)
from market import mark_iv, option_expiry
from core.straddle_grid import grid_unit
from collect_combos import ARCHIVE
from datetime import date, timedelta

class Packages(Study):
    def __init__(self, continuous=False):
        super().__init__()
        self.history = defaultdict(list)
        self.by_time = {}
        self.hours = []
        self.raw_count = 0
        first = date.fromisoformat(START)
        for j in range((date.fromisoformat(END)-first).days):
            dt = first+timedelta(days=j)
            if not continuous and dt.weekday()>=4:
                continue
            day = dt.isoformat()
            source = HERE/'combo_full' if continuous else ARCHIVE
            with gzip.open(source/(day+'.json.gz'),'rt') as f:
                data = json.load(f)
            self.raw_count += data['all_combo_trades']
            for row in data['rows']:
                self.history[row[1]].append(row)
                self.by_time.setdefault(row[0]//HOUR*HOUR,[]).append(row)
            base = timestamp(day)
            self.hours.extend(base+i*HOUR for i in (range(24) if continuous else range(12,18)))
        self.stamps = {k:[r[0] for r in v] for k,v in self.history.items()}

    @functools.lru_cache(None)
    def signal(self,t):
        spot = self.market.funding[t][0]
        snapshots = {}
        for prior in range(t-24*HOUR,t,HOUR):
            for row in self.by_time.get(prior,()):
                sym = row[1]
                if sym not in snapshots:
                    snapshots[sym] = [0.,None]
                snapshots[sym][0] += row[4]
                snapshots[sym][1] = row
        choices = []
        for sym,(vol,last) in snapshots.items():
            _,_,exp,k = sym.split('-')
            strike = float(k)
            expiry = option_expiry('BTC-'+exp+'-'+k+'-C')
            if abs(strike/spot-1)>.03 or not 6*HOUR<=expiry-t<=7*DAY:
                continue
            choices.append((-vol,abs(strike-spot),expiry,sym,strike,last))
        for _,__,expiry,sym,k,last in sorted(choices):
            stamp,_,_,_,_,s,_,marks,_ = last
            vols = [mark_iv((stamp,v*s,s,v*s,None,expiry,0,None),k,j==0) for j,v in enumerate(marks)]
            if any(v is None for v in vols):
                continue
            values = [greeks(spot,k,(expiry-t)/YEAR,v,j==0)[0] for j,v in enumerate(vols)]
            raw = .975*sum(values)/spot
            analogues = []
            for j in range(1,14):
                shift = j*7*DAY
                if expiry-shift>=t or t-shift not in self.market.funding:
                    continue
                past_settle = self.market.delivery.get(datestr(expiry-shift))
                if past_settle:
                    past_spot = self.market.funding[t-shift][0]
                    analogues.append(abs(spot*past_settle/past_spot-k)/spot)
            if len(analogues)<8:
                continue
            cap = .9*mean(sorted(analogues)[:-1])
            return dict(spot=spot,expiry=expiry,strike=k,symbol=sym,vols=vols,
                        limit=floor_lot(min(cap,raw),.0001), control=floor_lot(raw,.0001))
        return None

    def purchase_package(self,sig,t,budget,costs,control):
        limit = sig['control' if control else 'limit']
        for r in self.by_time.get(t,()):
            ts,sym,direction,p,amount,idx,legs,_,_ = r
            if sym!=sig['symbol'] or ts<t+MINUTE or direction!='sell' or limit-p<.0001-1e-12:
                continue
            if abs(sum(legs)-p)>1e-8 or min(legs)<0:
                continue
            leg_prices=[price+.5*(limit-p) for price in legs]
            cost=sum(price*idx+option_fee(price*idx,idx) for price in leg_prices)*(1+costs.fx)
            q=floor_lot(min(budget/sig['spot'],budget/idx,.08*budget/cost,costs.participation*amount),.1)
            if q:
                return r,limit,q,[(j,ts,idx,price,q) for j,price in enumerate(leg_prices)]
        return None

    def exits(self,sig,ts,limit,q,take,costs):
        left=q
        sales=[]
        expiry=sig['expiry']
        if take:
            sym=sig['symbol']
            ask=math.ceil(1.5*limit/.0001-1e-10)*.0001
            i=bisect_left(self.stamps[sym],(ts//HOUR+1)*HOUR+MINUTE)
            end=bisect_left(self.stamps[sym],expiry)
            for row in self.history[sym][i:end]:
                stamp,_,side,p,amount,idx,legs,_,_=row
                if stamp%HOUR<MINUTE or side!='buy' or p-ask<.0001-1e-12 or abs(sum(legs)-p)>1e-8 or min(legs)<0:
                    continue
                n=floor_lot(min(left,costs.participation*amount),.1)
                if n:
                    for j,px in enumerate(legs):
                        sales.append((j,stamp,idx,ask*px/p*idx,n,False))
                    left=round(left-n,10)
                if not left:
                    break
        if left:
            settle=self.market.delivery[datestr(expiry)]
            for j in (0,1):
                value=max((settle-sig['strike'])*(1 if j==0 else -1),0.)
                sales.append((j,expiry,settle,value,left,True))
        return sales

    def run_packages(self,book,lo,hi,costs=BASE,omit=None,*,initial_capital=CAPITAL):
        nav=initial_capital
        until=timestamp(lo)
        right=timestamp(hi)
        rows=[]
        attempts=0
        for t in self.hours:
            if t<until or t>=right:
                continue
            sig=self.signal(t)
            if not sig or sig['expiry']>right:
                continue
            attempts+=1
            trade=self.purchase_package(sig,t,nav,costs,book=='mark_control')
            if not trade:
                continue
            print_row,limit,q,fills=trade
            ts=print_row[0]
            expiry=sig['expiry']
            until=expiry
            if t==omit:
                continue
            sales=self.exits(sig,ts,limit,q,book=='take_profit',costs)
            opt=self.market_value(fills,sales,costs)
            start=(ts//MINUTE+1)*MINUTE
            stop=expiry-30*MINUTE
            hedge,n,closed,marks,parts=0.,0,expiry,{},{}
            if book in ('combo','delta') and start<stop:
                anchor,h=self.levels(start)
                gamma=sum(greeks(sig['spot'],sig['strike'],(expiry-t)/YEAR,v,j==0)[2] for j,v in enumerate(sig['vols']))
                unit=grid_unit(nav,anchor,q,gamma,h)
                if unit or book=='delta':
                    hedge,n,closed,marks,parts=self.overlay(book,start,stop,unit,t,q,costs)
            closed=max(closed,expiry)
            if closed>right:
                raise ValueError('Liquidation outside sample')
            marks=dict(marks)
            for u in range((t//DAY+1)*DAY,closed+1,DAY):
                seen=[sale for sale in sales if sale[1]<=u]
                if u<expiry:
                    idx=self.market.funding[u][0]
                    for j in (0,1):
                        unsold=q-sum(sale[4] for sale in seen if sale[0]==j)
                        px=greeks(idx,sig['strike'],(expiry-u)/YEAR,sig['vols'][j],j==0)[0]
                        if unsold>1e-9:
                            seen.append((j,u,idx,px,unsold,False))
                marks[u]=marks.get(u,hedge if u>=stop else 0.)+self.market_value(fills,seen,costs)
            pnl=opt+hedge
            rows.append(dict(opened=t,closed=closed,label=datestr(t),before=nav,after=nav+pnl,pnl=pnl,
                             marks=sorted(marks.items()),option_pnl=opt,perp_pnl=hedge,perp_fills=n,
                             parts=parts,qty=q,bid_btc=limit,fill=print_row,buy_fills=fills,sales=sales))
            until=closed
            nav+=pnl
        return rows,attempts
