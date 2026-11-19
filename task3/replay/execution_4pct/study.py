"""Tape-based fills, annual account replay and execution controls. Python 3.10+."""
import csv
import functools
import json
import math
import sys
from bisect import bisect_right
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'flow_curve_2024'))
from common import CAPITAL, CUT, DAY, END, HOUR, MINUTE, START, YEAR, ci, datestr, run_market, summary, timestamp
from options import window
from archive import windows
from engine import choose_pair
from market import greeks
from backtest import Perpetual
from strategies import TradingCosts, STRESS, floor_lot, option_bid, option_fee, qualifying_volume, tick_btc
from core.straddle_grid import inventory_quotes, passive_grid_fill
from core.straddle_delta import hedge_target
from core.straddle_grid import grid_unit

BASE = TradingCosts()
BOOKS = ('combo', 'grid', 'delta', 'plain', 'profit_take', 'grid_zero_skew', 'delta_center')

class Study:
    def __init__(self):
        self.market = run_market()
        self.windows = [t for t, _ in windows()]

    @functools.lru_cache(None)
    def signal(self, t):
        data = window(t)
        spot = self.market.funding[t][0]
        expiry = data['expiry']
        pair = choose_pair(data['pre'], expiry, t, spot)
        if not pair:
            return None
        strike, names, vols = pair
        g = [greeks(spot, strike, (expiry - t)/YEAR, v, j == 0) for j, v in enumerate(vols)]
        bids = [option_bid(x[0]/spot) for x in g]
        if min(bids) <= 0:
            return None
        return dict(spot=spot, strike=strike, names=names, vols=vols, expiry=expiry, bids=bids)

    def purchase(self, t, sig, nav, costs):
        s, bids = sig['spot'], sig['bids']
        budget = .08 * nav
        est = sum(p*s + option_fee(p*s,s) for p in bids) * (1+costs.fx)
        target = floor_lot(min(nav/s, budget/est), .1)
        qty = [0.,0.]
        fills = []
        for row in window(t)['post']:
            name, stamp, _, idx = row[:4]
            if not t + MINUTE <= stamp < t+30*MINUTE or name not in sig['names']:
                continue
            j = sig['names'].index(name)
            p = bids[j]
            available = qualifying_volume(row, p, 'sell', costs.participation)
            debit = (p*idx + option_fee(p*idx,idx)) * (1+costs.fx)
            q = floor_lot(min(available, target-qty[j], budget/debit), .1)
            if q:
                fills.append((j, stamp, idx, p, q))
                budget -= q*debit
                qty[j] = round(qty[j]+q, 10)
        return qty, fills

    def option_account(self, t, sig, fills, qty, take, costs):
        expiry = sig['expiry']
        remaining = list(qty)
        closes = []
        if take:
            for u in self.windows[bisect_right(self.windows,t):]:
                if u >= expiry or not any(remaining):
                    break
                for row in window(u)['post']:
                    name, stamp, _, idx = row[:4]
                    if not u+MINUTE <= stamp < u+30*MINUTE or name not in sig['names']:
                        continue
                    j = sig['names'].index(name)
                    raw = 2*sig['bids'][j]
                    ask = math.ceil(raw/tick_btc(raw)-1e-10)*tick_btc(raw)
                    q = min(remaining[j], qualifying_volume(row, ask, 'buy', costs.participation))
                    if q:
                        closes.append((j, stamp, idx, ask*idx, q, False))
                        remaining[j] = round(remaining[j]-q, 10)
        settle = self.market.delivery[datestr(expiry)]
        for j, q in enumerate(remaining):
            if q:
                value = max((settle-sig['strike'])*(1 if j==0 else -1),0.)
                closes.append((j, expiry, settle, value, q, True))

        leg_pnl = [0.,0.]
        costs_paid = 0.
        # FIFO attribution of financing to each premium until that quantity is sold.
        inventory = [[],[]]
        for j, stamp, idx, p, q in fills:
            gross = p*idx
            fee = option_fee(gross,idx)
            debit = (gross+fee)*(1+costs.fx)
            leg_pnl[j] -= q*debit
            costs_paid += q*(debit-gross)
            inventory[j].append([q,stamp,gross+fee])
        for j, stamp, idx, gross, q, delivery in closes:
            fee = option_fee(gross,idx,delivery)
            credit = (gross-fee)*(1-costs.fx)
            leg_pnl[j] += q*credit
            costs_paid += q*(gross-credit)
            left = q
            while left > 1e-9:
                lot = inventory[j][0]
                used = min(lot[0], left)
                financing = used*lot[2]*costs.carry*(stamp-lot[1])/YEAR
                leg_pnl[j] -= financing
                costs_paid += financing
                left -= used
                lot[0] -= used
                if lot[0] < 1e-9:
                    inventory[j].pop(0)
        return leg_pnl, costs_paid, closes

    @functools.lru_cache(maxsize=150000)
    def levels(self, t):
        bars = self.market.bars
        prices = [bars[t-j*5*MINUTE-MINUTE][3] for j in range(13)]
        vol = math.sqrt(sum(math.log(a/b)**2 for a,b in zip(prices,prices[1:])))
        return prices[0], prices[0]*max(.0015,.35*vol)

    @functools.lru_cache(maxsize=24000)
    def overlay(self, book, start, stop, size, sig_time, matched, costs):
        m = self.market
        p = Perpetual()
        u = start
        last = m.bars[u-MINUTE][3]
        anchor, halfwidth = self.levels(start)
        is_grid = book in ('combo','grid','grid_zero_skew')
        sig = self.signal(sig_time) if sig_time else None
        pending = None
        marks = []
        while u < stop or abs(p.position) > 1e-8:
            op, hi, lo, close, volume = m.bars[u]
            after = u+MINUTE
            hour_end = ((u//HOUR)+1)*HOUR
            index, rate = m.funding[hour_end]
            p.funding -= p.position*index*rate/60
            p.gross += p.position*(close-last)
            p.capital += abs(p.position)*last*costs.margin*costs.carry*MINUTE/YEAR
            flatten = u >= stop
            if flatten or pending is not None:
                target = 0. if flatten else pending
                dq = target-p.position
                amount = floor_lot(min(abs(dq), volume*costs.participation),.001)
                if amount:
                    move = math.copysign(amount,dq)
                    fill = max(op,close) if move>0 else min(op,close)
                    p.order(move,fill,close,costs.taker_bp,costs.impact_bp)
                if abs(target-p.position)<1e-8:
                    pending = None
            if not flatten and is_grid:
                if u % (5*MINUTE) == 0:
                    anchor,halfwidth = self.levels(u)
                skew = 0. if book=='grid_zero_skew' else .5
                quotes = inventory_quotes(anchor,halfwidth,p.position,size,skew,last)
                trade = passive_grid_fill(quotes,hi,lo,volume,size,costs)
                if trade:
                    dq, px = trade
                    p.order(dq,px,close,costs.maker_bp)
            if not flatten and not is_grid:
                hour = u//HOUR*HOUR
                spot = close - (m.bars[hour-MINUTE][3] - m.funding[hour][0])
                tau = (sig['expiry']-after)/YEAR
                delta = sum(greeks(spot,sig['strike'],tau,v,j==0)[1] for j,v in enumerate(sig['vols']))
                pending = hedge_target(delta,matched,p.position,book=='delta_center')
            if after % DAY == 0:
                marks.append((after,p.pnl()))
            last = close
            u = after
        return p.pnl(),p.fills,u,dict(marks),p.values()

    def run(self, book, lo, hi, costs=BASE, omit=None):
        left,right = timestamp(lo),timestamp(hi)
        nav, until = CAPITAL, left
        rows = []
        standalone = book in ('grid','grid_zero_skew')
        times = range(left+12*HOUR,right,DAY) if standalone else self.windows
        attempts = 0
        for t in times:
            if t < until or t >= right or t%DAY != 12*HOUR:
                continue
            matched = 0.
            q = [0.,0.]
            opt, orphan, opt_cost, sold = 0.,0.,0.,0.
            fills, exits, details = [], [], {}
            if standalone:
                if t+6*HOUR>=right:
                    continue
                attempts += 1
                start, stop = t,t+6*HOUR
                unit = floor_lot(.10*nav/self.market.bars[t-MINUTE][3]/3,.001)
                if not unit or t == omit:
                    continue
            else:
                sig = self.signal(t)
                if not sig or sig['expiry']>right:
                    continue
                attempts += 1
                q, fills = self.purchase(t,sig,nav,costs)
                if not fills:
                    continue
                until = sig['expiry']
                if t == omit:
                    continue
                legs,opt_cost,exits = self.option_account(t,sig,fills,q,book=='profit_take',costs)
                sold = sum(row[4] for row in exits if not row[-1])
                opt = sum(legs)
                matched = min(q)
                orphan = sum(value*(n-matched)/n for value,n in zip(legs,q) if n)
                start,stop = t+30*MINUTE,sig['expiry']-30*MINUTE
                anchor,h = self.levels(start)
                gamma = sum(greeks(sig['spot'],sig['strike'],(sig['expiry']-t)/YEAR,v,j==0)[2] for j,v in enumerate(sig['vols']))
                unit = grid_unit(nav,anchor,matched,gamma,h)
                details = dict(legs=sig['names'],qty=q,matched=matched,bids=sig['bids'],leg_pnl=legs,
                               buy_fills=fills,exit_fills=exits,early_sold=sold)
            hedge, n, closed, marks, perp_parts = 0.,0,stop,{},{}
            if (standalone and unit) or (book=='combo' and unit) or (book in ('delta','delta_center') and matched):
                hedge,n,closed,marks,perp_parts = self.overlay(book,start,stop,unit,t if not standalone else 0,matched,costs)
            if standalone and not n:
                continue
            marks = dict(marks)
            if not standalone:
                closed = max(closed,sig['expiry'])
                # Hold books mark all current quantities at opening IV until settlement.
                # For take-profit, reflect each sale and finance only remaining open lots.
                for u in range((t//DAY+1)*DAY,closed+1,DAY):
                    if u>=sig['expiry']:
                        marks[u] = marks.get(u,hedge)+opt
                        continue
                    idx = self.market.funding[u][0]
                    assets = []
                    for j,num in enumerate(q):
                        sales = [e for e in exits if e[0]==j and e[1]<=u]
                        rem = round(num-sum(e[4] for e in sales),10)
                        value = greeks(idx,sig['strike'],(sig['expiry']-u)/YEAR,sig['vols'][j],j==0)[0]
                        if rem:
                            assets.append((j,u,idx,value,rem,False))
                    # A synthetic liquidation mark uses the same exit FX/fees as a tradable sale.
                    known = [e for e in exits if e[1]<=u] + assets
                    mark = self.market_value(fills,known,costs)
                    marks[u] = marks.get(u,hedge if u>=stop else 0.) + mark
            pnl = opt+hedge
            assert nav+pnl>0 and closed<=right
            rows.append(dict(opened=t,closed=closed,label=datestr(t),before=nav,after=nav+pnl,pnl=pnl,
                             marks=sorted(marks.items()),perp_pnl=hedge,perp_fills=n,perp_parts=perp_parts,
                             option_pnl=opt,orphan_pnl=orphan,option_costs=opt_cost,**details))
            until = max(until,closed)
            nav += pnl
        return rows,attempts

    @staticmethod
    def market_value(fills, closes, costs):
        value = 0.
        for j in (0,1):
            lots = []
            for leg,t,idx,price,q in fills:
                if leg != j:
                    continue
                debit = price*idx+option_fee(price*idx,idx)
                value -= q*debit*(1+costs.fx)
                lots.append([q,t,debit])
            for leg,t,idx,price,q,delivery in closes:
                if leg != j:
                    continue
                value += q*(price-option_fee(price,idx,delivery))*(1-costs.fx)
                while q>1e-9:
                    amount = min(q,lots[0][0])
                    value -= amount*lots[0][2]*costs.carry*(t-lots[0][1])/YEAR
                    lots[0][0] -= amount
                    q -= amount
                    if lots[0][0]<1e-9:
                        lots.pop(0)
        return value

def print_account(book):
    if book=='grid':
        replay = Study().run
    else:
        from packages import Packages
        replay = Packages(continuous=True).run_packages
    rows,_ = replay(book,CUT,END)
    metrics = summary(rows,CUT,END)
    metrics.pop('rank_ex_best')
    best = max(rows,key=lambda r:r['pnl'],default=None)
    without,_ = replay(book,CUT,END,omit=best['opened']) if best else (rows,0)
    metrics['cagr_ex_best'] = summary(without,CUT,END)['cagr']
    print(json.dumps(metrics,indent=2))

def main():
    model = Study()
    result = {}
    for book in BOOKS:
        partitions = {}
        for label,lo,hi in [('early',START,CUT),('recent',CUT,END)]:
            rows,attempts = model.run(book,lo,hi)
            metrics = summary(rows,lo,hi)
            best = max(rows,key=lambda r:r['pnl'],default=None)
            ex,_ = model.run(book,lo,hi,omit=best['opened']) if best else (rows,0)
            excluded = summary(ex,lo,hi)
            stress,_ = model.run(book,lo,hi,STRESS)
            metrics.update(attempts=attempts,best=best['label'] if best else None,
                           ex_best=excluded['cagr'],annual_ex_best=excluded['annual'],
                           stress_cagr=summary(stress,lo,hi)['cagr'],ci=ci(rows,lo,hi),
                           paired=sum(r.get('matched',0)>0 for r in rows),
                           matched_qty=sum(r.get('matched',0) for r in rows),
                           orphan_usdc=sum(r['orphan_pnl'] for r in rows),
                           option_usdc=sum(r['option_pnl'] for r in rows),
                           perp_usdc=sum(r['perp_pnl'] for r in rows),
                           early_sold=sum(r.get('early_sold',0) for r in rows))
            partitions[label] = dict(metrics=metrics, cycles=rows)
            print(book,label,metrics['annual'],metrics['cagr'],metrics['ex_best'],metrics['n'],flush=True)
        result[book]=partitions
    (HERE/'results.json').write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    with (HERE/'annual.csv').open('w',newline='') as fp:
        wr = csv.writer(fp)
        wr.writerow(['book','period','year','return','return_ex_best','cagr','cagr_ex_best','stress_cagr','cycles'])
        for book,periods in result.items():
            for label,data in periods.items():
                r = data['metrics']
                for y,v in r['annual'].items():
                    wr.writerow([book,label,y,v,r['annual_ex_best'][y],r['cagr'],r['ex_best'],r['stress_cagr'],r['n']])

if __name__=='__main__':
    main()
