"""Capital accounting for independent USDC portfolios on a continuous UTC clock."""
import csv
import datetime as dt
import functools
import hashlib
import json
import math
from pathlib import Path
import random
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'weekday_2024'))
from engine import CAPITAL, CONFIG, STRESS, CUT, START, END, DAY, HOUR, MINUTE, YEAR, datestr, timestamp
from research import run_market

RESULTS = HERE / 'results'
RESULTS.mkdir(exist_ok=True)

def save(name, value):
    (RESULTS / name).write_text(json.dumps(value, indent=1, ensure_ascii=False, allow_nan=False))

def read_result(name):
    return json.loads((RESULTS / name).read_text())

def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

@functools.lru_cache(maxsize=None)
def year_boundaries(lo, hi):
    return {timestamp(f'{y}-01-01'): str(y-1) for y in range(int(lo[:4])+1,int(hi[:4])+1)} | {timestamp(hi):str(int(hi[:4])-(hi.endswith('01-01')))}

def summary(rows, lo, hi):
    years = (timestamp(hi)-timestamp(lo))/YEAR
    end = rows[-1]['after'] if rows else CAPITAL
    peak = CAPITAL
    draw = 0.0
    for r in rows:
        for v in [r['before']+p for _,p in r['marks']]+[r['after']]:
            peak=max(peak,v); draw=max(draw,1-v/peak)
    annual={};prev=CAPITAL
    from bisect import bisect_right
    exits=[r['closed'] for r in rows]
    for boundary,year in sorted(year_boundaries(lo,hi).items()):
        ix=bisect_right(exits,boundary)
        nav=rows[ix-1]['after'] if ix else CAPITAL
        if ix<len(rows) and rows[ix]['opened']<boundary<rows[ix]['closed']:
            nav=rows[ix]['before']+dict(rows[ix]['marks'])[boundary]
        annual[year]=nav/prev-1;prev=nav
    assert math.isclose(prev,end,abs_tol=.001)
    rets=[r['pnl']/r['before'] for r in rows]
    worst_removed=sorted(rets,reverse=True)[1:]
    return dict(cagr=(end/CAPITAL)**(1/years)-1, annual=annual, equity=end, n=len(rows), wins=sum(r['pnl']>0 for r in rows),
                dd_daily_and_exit=draw,rank_ex_best=math.expm1(sum(map(math.log1p,worst_removed))/years))

def ci(rows,lo,hi):
    days=(timestamp(hi)-timestamp(lo))/DAY
    weeks=[0.]*math.ceil(days/7)
    for r in rows:
        k=min(len(weeks)-1,int((r['closed']-timestamp(lo))//(7*DAY)))
        weeks[k]+=math.log(r['after']/r['before'])
    rng=random.Random(744);n=len(weeks);sample=[]
    for _ in range(5000):
        sims=[]
        while len(sims)<n:
            k=rng.randrange(n);sims.extend(weeks[(k+j)%n] for j in range(8))
        sample.append(math.expm1(sum(sims[:n])*365/days))
    sample.sort();return [sample[124],sample[4874]]

def select(book,scores):
    scores.sort(key=lambda r:(r['eligible'],r['rank_ex_best'],r['cagr']),reverse=True)
    name=f'{book}-training.json';save(name,scores)
    frozen={'rule':scores[0]['rule'],'eligible':scores[0]['eligible'],'training_sha256':checksum(RESULTS/name),'protocol_sha256':checksum(HERE/'protocol.json')}
    if book.startswith(('path-','tenor-')):
        supplemental='path-protocol.json' if book.startswith('path-') else 'tenor-protocol.json'
        frozen['supplemental_protocol_sha256']=checksum(HERE/supplemental)
    save(f'{book}-frozen.json',frozen)
    print(book,'CHOSEN',scores[0],flush=True)

def detailed(run,book,rule):
    records={}
    for lo,hi,label in [(CUT,END,'test'),(START,CUT,'train'),(START,END,'full')]:
        rows=run(book,rule,lo,hi);s=summary(rows,lo,hi)
        if rows:
            usd=sorted(rows,key=lambda r:r['pnl'],reverse=True);pct=sorted(rows,key=lambda r:r['pnl']/r['before'],reverse=True)
            def remove(rs):return summary(run(book,rule,lo,hi,skip={r['opened'] for r in rs}),lo,hi)
            ex=remove(usd[:1]);s.update(best=usd[0]['label'],best_pnl=usd[0]['pnl'],ex_best=ex['cagr'],annual_ex_best=ex['annual'],ex_best_pct=remove(pct[:1])['cagr'],ex_three=remove(pct[:3])['cagr'],ci=ci(rows,lo,hi))
            s['stress']=summary(run(book,rule,lo,hi,stress=True),lo,hi)['cagr']
        records[label]={'summary':s,'cycles':rows}
        print(book,label,s,flush=True)
    save(f'{book}.json',records)
