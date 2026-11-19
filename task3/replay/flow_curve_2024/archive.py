"""Collect short point-in-time BTC tape windows around the fixed decision clock."""
import concurrent.futures
import datetime as dt
import gzip
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'weekday_2024'))
from collect import fetch

HOUR = 3600000
DAY = 24 * HOUR
TAPE = HERE / 'tape'

def windows():
    day = dt.date(2022,5,2)
    while day <= dt.date(2026,11,13):
        weekday = day.weekday()
        hours = (12,18) if weekday < 4 else (6,) if weekday == 4 else ()
        for hour in hours:
            now = dt.datetime.combine(day, dt.time(hour), dt.timezone.utc)
            expiry = day + dt.timedelta(days=(4-weekday) % 7)
            yield (int(now.timestamp()*1000), expiry)
        day += dt.timedelta(days=1)

def collect_window(job):
    t, expiry = job
    path = TAPE / f'{t}.json.gz'
    if path.exists():return
    exp = int(dt.datetime.combine(expiry,dt.time(8),dt.timezone.utc).timestamp()*1000)
    front = expiry.strftime('%d%b%y').lstrip('0').upper()
    rows = fetch(t-2*HOUR, t+HOUR//2)
    front_pre, front_post, far = [], [], {}
    maturities = {}
    for r in rows:
        name=r['instrument_name']; bits=name.split('-')
        if len(bits)!=4 or bits[0]!='BTC' or r.get('liquidation') or r.get('combo_id') or r.get('block_trade_id'):continue
        if not all((r.get(k) or 0)>0 for k in ('price','amount','mark_price','index_price')):continue
        ts=r['timestamp']; item=[name,ts,r['mark_price'],r['index_price'],r['price'],r['direction'],r['amount'],str(r['trade_id'])]
        if bits[1]==front:
            (front_pre if ts<t else front_post).append(item)
        elif ts<t and abs(float(bits[2])/r['index_price']-1)<.06:
            if bits[1] not in maturities:
                date=dt.datetime.strptime(bits[1],'%d%b%y').replace(tzinfo=dt.timezone.utc,hour=8)
                maturities[bits[1]]=int(date.timestamp()*1000)
            ex=maturities[bits[1]]
            if 14*DAY<=ex-t<=49*DAY and (name not in far or ts>=far[name][1]):far[name]=item
    data={'time':t,'expiry':exp,'pre':sorted(front_pre,key=lambda r:r[1]),'post':sorted(front_post,key=lambda r:r[1]),'far':sorted(far.values(),key=lambda r:r[1]),'expiries':maturities}
    tmp=path.with_suffix('.part');tmp.write_bytes(gzip.compress(json.dumps(data,separators=(',',':')).encode(),mtime=0));tmp.replace(path)

if __name__=='__main__':
    TAPE.mkdir(exist_ok=True)
    jobs=[j for j in windows() if not (TAPE/f'{j[0]}.json.gz').exists()]
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        for n,_ in enumerate(pool.map(collect_window,jobs),1):
            if n%100==0 or n==len(jobs):print(n,'/',len(jobs),flush=True)
