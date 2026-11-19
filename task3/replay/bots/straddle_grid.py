"""Purchased ATM straddle plus grid. With size=0, the position is a plain straddle."""
import math
from market import YEAR, greeks
from .grid import order_size

def signal(past_indices, premium):
    return (max(past_indices)-min(past_indices))/premium

def implied_straddle(underlying,strike,tau,premium):
    lo,hi=.001,5.
    for _ in range(32):
        mid=(lo+hi)/2
        value=sum(greeks(underlying,strike,tau,mid,call)[0] for call in (True,False))
        if value<premium:lo=mid
        else:hi=mid
    return (lo+hi)/2

def opening_orders(entry):
    """Both option instructions belong to the same pair. No one-sided entry is intended."""
    return [{'symbol':symbol,'side':'buy','quantity':entry['units'],'limit_btc':price/entry['underlying']}
            for symbol,price in zip(entry['names'],entry['asks'])]

def floor_lot(x, lot):
    return math.floor(x / lot + 1e-10)*lot

def configure(e, cfg, equity):
    e = dict(e)
    s,k = e['underlying'],e['strike']
    tau=(e['expiry']-e['t'])/YEAR
    mids=[greeks(s,k,tau,v,i==0)[0] for i,v in enumerate(e['iv'])]
    asks=[]
    for mid in mids:
        raw=(mid+max(.00005*s,cfg.option_half_spread*mid))/s
        tick=.0001 if raw<=.005 else .0005
        asks.append(math.ceil(raw/tick-1e-12)*tick*s)
    prem=sum(asks)
    fee=sum(min(.0003*s,.125*x) for x in asks)
    debit=(prem+fee)*(1+cfg.fx_rate)
    units=floor_lot(min(.08*equity/debit, equity/s),.1)
    e['step']=round(s*cfg.step_bp*1e-4/cfg.perp_tick)*cfg.perp_tick
    e['center']=round((e['f0']-k)/e['step'])
    n=math.floor(cfg.width*prem/e['step'])
    if units<=0 or n<=abs(e['center']):
        return None
    order=order_size(units,e['gamma'],e['step'],n,e['center'],cfg.size,cfg.cap_btc,cfg.perp_lot)
    e.update(units=units,spread=prem-e['mid'],premium=prem,option_fee=fee,asks=asks,
             effective_iv=implied_straddle(s,k,tau,prem+fee),quantity=order,wings=n)
    return e
