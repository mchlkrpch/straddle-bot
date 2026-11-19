"""Two-level linear perpetual grid. Frozen January settings, with tape-verified maker fills."""
import gzip
import json
import math
import mmap
from pathlib import Path
from struct import Struct

from study import CAPITAL, CUT, END, DAY, MINUTE, timestamp, datestr, summary

# width, timeout (minutes), required last-trade dislocation; prices expressed in fractions
SETTINGS = {2024: (.0015, 20, .0025), 2025: (.012, 20, 0.), 2026: (.012, 50, 0.)}
FIRST = timestamp('2022-04-24')
BAR = Struct('<7d')

def quotes(position, unit, fair, prev, traded, first_price, width, gate):
    """Return passive bid, ask, and size per side (in lots of 0.001 BTC)."""
    bid = ask = 0
    gap = prev / fair - 1
    if not position:
        if not gate:
            bid, ask = math.floor(fair*(1-width)), math.ceil(fair*(1+width))
        elif traded and gap < -gate:
            bid = math.floor(prev*(1-width))
        elif traded and gap > gate:
            ask = math.ceil(prev*(1+width))
    elif position > 0:
        ask = math.ceil(fair)
        if position == unit and (not gate or gap < -gate):
            bid = math.floor(first_price*(1-width) if gate else fair*(1-2*width))
    else:
        bid = math.floor(fair)
        if position == -unit and (not gate or gap > gate):
            ask = math.ceil(first_price*(1+width) if gate else fair*(1+2*width))
    return bid, ask, -position if position < 0 else unit, position if position > 0 else unit

def run(bars, tape, omit=None):
    nav = CAPITAL
    rows = []
    def bar(t):
        return BAR.unpack_from(bars, (t-FIRST)//MINUTE * BAR.size)
    for year, (width, timeout, gate) in SETTINGS.items():
        start = timestamp(f'{year}-01-01')
        end = min(timestamp(f'{year+1}-01-01'), timestamp(END))
        busy = start
        for hour in range(start,end,60*MINUTE):
            fair = bar(hour)[5]
            if not fair or hour<busy or (omit and omit[0]<=hour<omit[1]):
                continue
            unit = math.floor(.10*nav/fair/.002+1e-9)
            position = fills = 0
            gross = fees = slip = funding = carry = first_price = 0.
            marks = []
            t = hour+MINUTE
            while t<end and (t<hour+timeout*MINUTE or position):
                opening,high,low,close,volume,fair,rate = bar(t)
                prev,prior_volume = bar(t-MINUTE)[3:5]
                funding -= position*.001*rate
                carry += abs(position)*.001*prev*.10*.05/(365*1440)
                cap = math.floor(.25*volume/.001+1e-8)
                change = 0
                taker = t>=hour+timeout*MINUTE
                if taker:
                    change = min(abs(position),cap)*(1 if position<0 else -1)
                    price = max(opening,close) if change>0 else min(opening,close)
                elif fair:
                    bid,ask,buy,sell = quotes(position,unit,fair,prev,prior_volume>0,first_price,width,gate)
                    down = 0<bid<prev and low<bid*(1-.0002)
                    up = ask>prev and high>ask*(1+.0002)
                    if down != up:
                        if down and buy<=cap:
                            change,price = buy,bid
                        if up and sell<=cap:
                            change,price = -sell,ask
                    if change:
                        prints=tape[t]  # Missing tape is a data error, even for an unfilled order.
                        depth=sum(q for _,side,px,q,_ in prints if
                                  (change>0 and side=='sell' and px<price*(1-.0002)) or
                                  (change<0 and side=='buy' and px>price*(1+.0002)))
                        if .25*depth+1e-12 < abs(change)*.001:
                            change=0
                t += MINUTE
                if change:
                    if not position:
                        first_price = price
                    qty = change*.001
                    position += change
                    impact = .0002 if taker else 0.
                    gross -= qty*price
                    fees += abs(qty)*price*(1+math.copysign(impact,qty))*(.0005 if taker else .00015)
                    slip += abs(qty)*price*impact
                    fills += 1
                    if not position:
                        break
                if fills and t%DAY==0:
                    marks.append([t,gross-fees-slip+funding-carry+position*.001*close])
            if position:
                raise ValueError('Open inventory at year boundary')
            if fills:
                pnl=gross-fees-slip+funding-carry
                rows.append(dict(opened=hour,closed=t,label=datestr(hour),before=nav,after=nav+pnl,
                                 pnl=pnl,marks=marks,fills=fills))
                nav+=pnl
                busy=t
    return rows

def evaluate(folder, binary):
    tape={}
    for f in (folder/'maker_tape').glob('*.gz'):
        with gzip.open(f,'rt') as stream:
            tape[int(f.name.split('.')[0])]=json.load(stream)
    with open(binary,'rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        rows=run(data,tape)
        best=max(rows,key=lambda r:r['pnl'])
        ex=summary(run(data,tape,(best['opened'],best['closed'])),CUT,END)
    m=summary(rows,CUT,END)
    m.update(ex_best=ex['cagr'],annual_ex_best=ex['annual'])
    (folder/'grid_walk.json').write_text(json.dumps(dict(metrics=m,cycles=rows)))
