"""One daily decision, trend direction and volatility-dependent exposure. Simulation only."""
import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, stdev

from prepare import DAY, HOUR, STEP, read

ROOT = Path(__file__).resolve().parent
DEVELOPMENT_START = '2025-09-06'
DEVELOPMENT_MID = '2025-12-20'
SPLIT = '2026-04-10'
DERIBIT_END = '2026-11-14'
OKX_START = '2026-05-15'
OKX_END = '2026-10-01'
CAPITAL = 150_000.
FLOOR_VOL = .20
MARGIN = .10
CARRY = .05
SLIPPAGE_BP = 2.
MAX_EXPOSURE = 1.0
DD_LIMIT = .20
LOT = .0001


def ms(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def iso(t):
    return datetime.fromtimestamp(t/1000, timezone.utc).isoformat(timespec='minutes')


def clip(value, low, high):
    return min(max(value,low),high)


def record(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                    encoding='utf-8')


@dataclass(frozen=True)
class Parameters:
    horizon: int
    target_vol: float

    def __post_init__(self):
        if self.horizon <= 0 or not 0 < self.target_vol <= 1:
            raise ValueError('Invalid model parameters')


@dataclass(frozen=True)
class Signal:
    time: int
    price: float
    vol: float
    z: float
    weight: float


class Market:
    def __init__(self, path):
        self.path = path
        data = read(path)
        self.venue, self.instrument = data['venue'], data['instrument']
        self.fee = data['fee_bps'] / 10_000
        self.funding_mode = data['funding_mode']
        self.bars = data['bars']
        self.start = self.bars[0][0]
        self.end = self.bars[-1][0] + STEP
        self.funding = {t:(index, rate) for t,index,rate in data['funding']}
        if len(self.bars) != (self.end-self.start)//STEP or any(
                t != self.start+i*STEP or close<=0 or (op is not None and op<=0)
                for i,(t,op,close) in enumerate(self.bars)):
            raise ValueError('Noncontinuous or invalid market bars')
        hours = 8*HOUR if self.funding_mode=='point' else HOUR
        if any(t not in self.funding for t in range(self.start+hours,self.end+1,hours)):
            raise ValueError(f'Missing funding records in {self.venue}')
        self.closes = {t:self.bars[(t-self.start)//STEP-1][2]
                       for t in range(self.start+DAY,self.end+1,DAY)}
        self.forecasts = forecast(self.closes)


def forecast(closes):
    """EWMA of daily squared log returns with 7- and 30-day half-lives."""
    decay_fast, decay_slow = (2**(-1/days) for days in (7,30))
    previous = fast = slow = None
    result = {}
    for t, price in sorted(closes.items()):
        if previous is not None:
            variance = math.log(price/previous)**2
            fast = variance if fast is None else decay_fast*fast+(1-decay_fast)*variance
            slow = variance if slow is None else decay_slow*slow+(1-decay_slow)*variance
            result[t] = math.sqrt(365*(.65*fast+.35*slow))
        previous = price
    return result


def make_signal(market, t, params, exposure=None, buy_hold=False):
    price = market.closes[t]
    vol = market.forecasts[t]
    prior = market.closes[t-params.horizon*DAY]
    sigma = max(vol,FLOOR_VOL)
    z = math.log(price/prior)/(sigma*math.sqrt(params.horizon/365))
    limit = min(MAX_EXPOSURE,params.target_vol/sigma) if exposure is None else exposure
    weight = 1. if buy_hold else limit*clip(z,-1.,1.)
    return Signal(t,price,vol,z,weight)


def run(market, start, end, params, *, fixed_exposure=None, buy_hold=False,
        extra_bps=0., include_trail=False, terminal=True):
    left, right = ms(start), ms(end)
    if left <= market.start or right > market.end or (right-left)%DAY or left>=right:
        raise ValueError(f'Unsupported range {start}–{end} on {market.venue}')
    day_count = (right-left)//DAY
    positions, daily_equity, trail = [], [CAPITAL], []
    pnl = funding = fees = slip = carry = qty = hours_exposure = trade_notional = 0.
    turnover_trades = 0
    peak = CAPITAL
    max_dd = 0.
    price = market.closes[left]
    equity = CAPITAL
    final_quote = None
    pending = None

    def update_equity():
        nonlocal equity, peak, max_dd
        equity = CAPITAL + pnl + funding - fees - slip - carry
        if equity <= 0:
            raise ValueError('Account equity exhausted')
        peak = max(peak,equity)
        max_dd = max(max_dd,1-equity/peak)

    def mark(to_price):
        nonlocal price, pnl
        pnl += qty*(to_price-price)
        price = to_price

    def execute(target):
        nonlocal qty, fees, slip, trade_notional, turnover_trades
        value = abs(target-qty)*price
        if value>1e-8:
            trade_notional += value
            turnover_trades += 1
        fees += value*market.fee
        slip += value*(SLIPPAGE_BP+extra_bps)/10_000
        qty = target
        update_equity()

    first, last = (left-market.start)//STEP, (right-market.start)//STEP
    for t, opening, close in market.bars[first:last]:
        if t%DAY == 0:
            pending = (None if buy_hold and positions else
                       make_signal(market,t,params,fixed_exposure,buy_hold))
        if pending and t>=pending.time+STEP and opening is not None:
            mark(opening)
            update_equity()
            raw = abs(pending.weight)*equity/opening
            target = math.copysign(math.floor(raw/LOT+1e-10)*LOT,pending.weight)
            execute(target)
            positions.append(asdict(pending) | dict(fill_time=t, fill_price=opening, quantity=target,
                                                    equity=equity))
            pending = None
        mark(close)
        final_quote = close
        hours_exposure += qty/12
        carry += abs(qty)*close*MARGIN*CARRY*STEP/(365*DAY)
        stamp = t+STEP
        if stamp in market.funding:
            index, rate = market.funding[stamp]
            if market.funding_mode=='hourly_average':
                funding -= hours_exposure*index*rate
                hours_exposure = 0.
            else:
                funding -= qty*close*rate
        update_equity()
        if stamp%DAY == 0:
            daily_equity.append(equity)
        if include_trail and (stamp%DAY == 0 or stamp==right):
            trail.append(dict(time=stamp,equity=equity,gross=pnl,funding=funding,fees=fees,
                              slippage=slip,carry=carry,position=qty,price=price))

    if pending:
        raise ValueError('Last rebalance had no eligible market opening')
    if terminal:
        if final_quote is None:
            raise ValueError('No end price')
        execute(0.)
        daily_equity[-1] = equity
        if trail:
            trail[-1].update(equity=equity,fees=fees,slippage=slip,position=qty)

    returns = [b/a-1 for a,b in zip(daily_equity,daily_equity[1:])]
    deviation = stdev(returns) if len(returns)>1 else 0.
    risk = dict(total_return=equity/CAPITAL-1, cagr=(equity/CAPITAL)**(365/day_count)-1,
                max_drawdown=max_dd, volatility=deviation*math.sqrt(365),
                sharpe=mean(returns)/deviation*math.sqrt(365) if deviation else None,
                gross=pnl, funding=funding, fees=fees, slippage=slip, carry=carry,
                trades=turnover_trades, turnover=trade_notional, days=day_count,
                avg_abs_weight=mean(abs(p['weight']) for p in positions))
    assert math.isclose(CAPITAL+risk['gross']+risk['funding']-risk['fees']-risk['slippage']-risk['carry'],
                        equity,abs_tol=1e-7)
    return dict(stats=risk, daily=trail, decisions=positions if include_trail else [])


PROTOCOL = dict(development=[DEVELOPMENT_START,SPLIT], subperiod_split=DEVELOPMENT_MID,
                holdout=[SPLIT,DERIBIT_END], transfer=[OKX_START,OKX_END],
                horizons=[7,14,28,42], vol_targets=[.15,.25,.35,.45],
                max_development_drawdown=DD_LIMIT,
                selection='maximize dev CAGR subject to max drawdown and both subperiods positive; '
                          'if no two-positive fit, maximize worst subperiod under the same drawdown cap')


def calibrate(market, out):
    candidates = []
    for horizon in PROTOCOL['horizons']:
        for goal in PROTOCOL['vol_targets']:
            p = Parameters(horizon,goal)
            full = run(market,DEVELOPMENT_START,SPLIT,p)['stats']
            early = run(market,DEVELOPMENT_START,DEVELOPMENT_MID,p)['stats']
            later = run(market,DEVELOPMENT_MID,SPLIT,p)['stats']
            unit = run(market,DEVELOPMENT_START,SPLIT,p,fixed_exposure=1.)['stats']
            candidates.append(dict(params=asdict(p),dev=full, early=early['cagr'],later=later['cagr'],
                                   fixed_exposure=min(1.,full['avg_abs_weight']/unit['avg_abs_weight'])))
    allowed = [c for c in candidates if c['dev']['max_drawdown']<=DD_LIMIT]
    if not allowed:
        raise ValueError('Every early configuration exceeded the drawdown limit')
    positive = [c for c in allowed if min(c['early'],c['later'])>0]
    if positive:
        winner = max(positive,key=lambda c:(c['dev']['cagr'],-c['dev']['max_drawdown']))
    else:
        winner = max(allowed,key=lambda c:(min(c['early'],c['later']),c['dev']['cagr']))
    payload = dict(protocol=PROTOCOL,winner=winner,candidates=candidates)
    record(out/'selection.json',payload)
    return winner


def evaluate(deribit, okx, out):
    frozen = out/'selection.json'
    selected = json.loads(frozen.read_text())
    if selected['protocol']!=PROTOCOL:
        raise ValueError('Protocol differs from saved selection')
    winner=selected['winner']
    params=Parameters(**winner['params'])
    comparisons = {}
    for market, start, end in ((deribit,SPLIT,DERIBIT_END),(okx,OKX_START,OKX_END)):
        cases = dict(adaptive={}, fixed_risk={'fixed_exposure':winner['fixed_exposure']},
                     buy_hold={'buy_hold':True}, stress={'extra_bps':5.})
        comparisons[market.venue] = {}
        for name, overrides in cases.items():
            replay = run(market,start,end,params,include_trail=True,**overrides)
            comparisons[market.venue][name]=replay['stats']
            if name=='adaptive':
                for tag, rows in (('equity',replay['daily']),('decisions',replay['decisions'])):
                    with (out/f'{market.venue}_{tag}.csv').open('w',newline='',encoding='utf-8') as stream:
                        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
                        writer.writeheader()
                        writer.writerows(rows)
    result = dict(chosen=winner['params'],selection_sha256=hashlib.sha256(frozen.read_bytes()).hexdigest(),
                  results=comparisons)
    record(out/'holdout.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('select','holdout','latest'))
    p.add_argument('--data',type=Path,default=ROOT/'data')
    p.add_argument('--out',type=Path,default=ROOT/'results')
    p.add_argument('--venue',choices=('deribit','okx'),default='deribit')
    a=p.parse_args()
    if a.command=='select':
        result=calibrate(Market(a.data/'deribit.json.gz'),a.out)
    elif a.command=='holdout':
        result=evaluate(Market(a.data/'deribit.json.gz'),Market(a.data/'okx.json.gz'),a.out)
    else:
        market=Market(a.data/(a.venue+'.json.gz'))
        saved=json.loads((a.out/'selection.json').read_text())
        date=max(market.closes)
        signal=make_signal(market,date,Parameters(**saved['winner']['params']))
        result=asdict(signal)|dict(asof=iso(date),execution='paper target on first eligible bar after 00:05 UTC')
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
