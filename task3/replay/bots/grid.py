"""Standalone neutral inventory grid. Uses the reference option premium and gamma to size orders."""
import math
LOT=.0001
TICK=.1

def order_size(units,gamma,step,wings,center,size,cap,lot=LOT):
    raw=units*min(size*gamma*step,cap/(wings+abs(center)))
    return math.floor(raw/lot+1e-10)*lot

def resting_limits(strike,level,step,wings,previous):
    """Prices known before the next bar. None means the side is inactive."""
    buy=round((strike+(level-1)*step)/TICK)*TICK
    sell=round((strike+(level+1)*step)/TICK)*TICK
    return (buy if level>-wings and buy<previous else None,
            sell if level<wings and sell>previous else None)

def passive_order(strike,level,step,wings,quantity,previous,high,low,volume,through_bp,inventory=0.,max_levels=0):
    """Return (signed change, limit price, ambiguous). Positive quantity means buy BTC."""
    if volume<quantity or quantity<=0:
        return 0.,0.,False
    buy,sell=resting_limits(strike,level,step,wings,previous)
    if max_levels and inventory >= (max_levels-.5)*quantity:
        buy=None
    if max_levels and inventory <= -(max_levels-.5)*quantity:
        sell=None
    through=through_bp/1e4
    down=buy is not None and low<buy*(1-through)
    up=sell is not None and high>sell*(1+through)
    if down and up:
        return 0.,0.,True
    if down:
        return quantity,buy,False
    if up:
        return -quantity,sell,False
    return 0.,0.,False

def boundary_reduction(position,percent=100):
    lots=round(abs(position)/LOT)*percent//100
    return -math.copysign(lots*LOT,position) if lots else 0.

def choppy_week(past_indices):
    """Eight daily observations, latest first. Low net movement relative to path length."""
    moves=[math.log(a/b) for a,b in zip(past_indices,past_indices[1:])]
    length=sum(abs(move) for move in moves)
    return length>0 and abs(sum(moves))/length<=1/3
