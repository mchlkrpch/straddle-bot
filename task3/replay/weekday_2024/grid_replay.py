"""Standalone grid, closing on the first traded open from 30 minutes before deadline."""
import math
from backtest import Perpetual, BREAKOUT_POLICIES
from bots.grid import passive_order, boundary_reduction
from market import MINUTE,DAY

def simulate(m,e,cfg,end):
    book=Perpetual(); t=e['t']; prev=e['f0']; strike=e['strike']; level=e['center']; step=e['step']; wings=e['wings']; size=e['quantity']; low=strike-wings*step; high=strike+wings*step
    stop=end-30*MINUTE; active=True;pending=0.; daily=[(t,0.)]
    while t<end or abs(book.position)>1e-9:
        opening,h,l,close,volume=m.bars[t]
        if t>=stop:
            active=False;pending=-book.position
        if volume>0 and abs(pending)>1e-9:
            book.order(pending,opening,prev,cfg.taker_bp,cfg.slippage_bp);pending=0.
        nxt=t+MINUTE
        book.carry_minute(prev,close,m.funding.get(nxt),cfg)
        if active:
            change,price,_=passive_order(strike,level,step,wings,size,prev,h,l,volume,cfg.trade_through_bp,book.position,cfg.inventory_levels)
            if change:
                book.order(change,price,close,cfg.maker_bp);level+=-1 if change>0 else 1
            if close<low or close>high:
                active=False;pending=boundary_reduction(book.position,BREAKOUT_POLICIES[cfg.breakout])
        t=nxt;prev=close
        if t%DAY==0:daily.append((t,book.pnl()))
    daily.append((t,book.pnl()));parts=book.values();parts['net']=book.pnl()
    return dict(pnl=book.pnl(),parts=parts,fills=book.fills,daily=daily,actual_close=t)
