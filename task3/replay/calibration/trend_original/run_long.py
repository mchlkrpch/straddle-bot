"""Replay the previously selected bot on longer Deribit data, without fitting."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from statistics import median

from core.trend import (CAPITAL, DERIBIT_END, DEVELOPMENT_START, ROOT, Market, Parameters,
                 SPLIT, iso, record, run)

FIRST_TRADE = '2022-05-01'
FROZEN_BOT = 'a87b94ddc087d553fc1dd89d374c319803abf5b0431e31c0e54d8d125e5b8e13'
FROZEN_SELECTION = 'cadb65da0b551ede1c347ee19b0225b4c8b875165dc136a34a324589b1c3b72c'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(market, start, end, parameters, **overrides):
    result = run(market,start,end,parameters,include_trail=True,**overrides)
    stats, trades = result['stats'], result['decisions']
    waits = [(row['fill_time']-row['time'])/60_000 for row in trades]
    equity = result['daily'][-1]['equity']
    return dict(start=start,end=end,**stats,pnl=equity-CAPITAL,ending_equity=equity,
                signals_executed=len(trades), median_wait_minutes=median(waits),
                max_wait_minutes=max(waits)), result['daily']


def years(trail):
    rows = []
    current = None
    basis = CAPITAL
    for day in trail:
        date = iso(day['time']-1)[:10]
        year = int(date[:4])
        if year!=current:
            if rows:
                basis = rows[-1]['ending_equity']
            rows.append(dict(year=year,start_equity=basis))
            current = year
        rows[-1].update(ending_equity=day['equity'],total_return=day['equity']/basis-1)
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=ROOT/'deribit_5m.json.gz')
    p.add_argument('--out',type=Path,default=ROOT/'results')
    a=p.parse_args()
    selection = ROOT/'selection.json'
    if digest(ROOT/'bot.py') != FROZEN_BOT or digest(selection) != FROZEN_SELECTION:
        raise ValueError('The saved bot or frozen selection differs from the original')
    frozen = json.loads(selection.read_text())['winner']['params']
    params = Parameters(**frozen)
    market = Market(a.data)
    if market.instrument != 'BTC_USDC-PERPETUAL' or market.fee != .00035:
        raise ValueError('Expected the original linear Deribit perpetual and fee model')
    intervals = dict(full=(FIRST_TRADE,DERIBIT_END), older=(FIRST_TRADE,DEVELOPMENT_START),
                     forward=(SPLIT,DERIBIT_END))
    results = {}
    for label, (start,end) in intervals.items():
        stats, trail = replay(market,start,end,params)
        results[label] = stats
        if label == 'full':
            full_curve = trail
    results['full_extra_5bps'],_ = replay(market,FIRST_TRADE,DERIBIT_END,params,extra_bps=5)

    a.out.mkdir(parents=True,exist_ok=True)
    with (a.out/'daily_equity.csv').open('w',newline='',encoding='utf-8') as file:
        writer = csv.DictWriter(file,fieldnames=list(full_curve[0]) + ['date_utc'])
        writer.writeheader()
        writer.writerows([day|{'date_utc':iso(day['time'])} for day in full_curve])
    result = dict(parameters=frozen,bot_sha256=FROZEN_BOT,selection_sha256=FROZEN_SELECTION,
                  instrument=market.instrument,initial_capital=CAPITAL,archive=dict(
                      start=iso(market.start),end=iso(market.end),bars=len(market.bars),
                      funding_hours=len(market.funding),inactive_bars=sum(op is None for _,op,_ in market.bars)),
                  results=results,yearly_returns=years(full_curve))
    record(a.out/'long_run.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
