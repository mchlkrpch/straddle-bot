"""Public Deribit campaign. Historical underlying inputs live in ../data at project install."""
import argparse
import concurrent.futures
import datetime as dt
import gzip
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DAY, HOUR = 86_400_000, 3_600_000
DIR = ROOT / 'observations'
def stamp(d):
    return int(dt.datetime.combine(d, dt.time(12), tzinfo=dt.timezone.utc).timestamp()*1000)

def fetch(left, right, tries=5):
    qs=urllib.parse.urlencode(dict(currency='BTC',kind='option',start_timestamp=left,
                              end_timestamp=right-1,count=10000,sorting='asc',include_old='true'))
    url='https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time?'+qs
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=35) as response:
                page=json.load(response)['result']
            break
        except (OSError,KeyError,ValueError) as e:
            if isinstance(e,urllib.error.HTTPError) and e.code in (401,403):raise
            if i==tries-1:raise
            time.sleep(2**i)
    if page['has_more']:
        mid=(left+right)//2
        if mid<=left:raise ValueError('saturated timestamp')
        return fetch(left,mid)+fetch(mid,right)
    return page['trades']

def collect(date):
    t=stamp(date)
    path=DIR/(date.isoformat()+'.json.gz')
    if path.exists():return
    expiry=date+dt.timedelta(days=(4-date.weekday())%7 or 7)
    token=expiry.strftime('%d%b%y').lstrip('0').upper()
    prefix='BTC-'+token+'-'
    raw=fetch(t-2*HOUR,t+HOUR)
    before={};after=[]
    for r in raw:
        name=r['instrument_name'];date_ms=r['timestamp']
        if not name.startswith(prefix) or r.get('block_trade_id') or r.get('combo_id') or r.get('liquidation'):continue
        if not all(r.get(k) is not None and float(r[k])>0 for k in ('index_price','price','mark_price','amount')):continue
        row=[name,date_ms,r['mark_price'],r['index_price'],r['price'],r['direction'],r['amount'],str(r['trade_id'])]
        if date_ms<t:
            if name not in before or before[name][1]<=date_ms:before[name]=row
        else:after.append(row)
    saved={'date':date.isoformat(),'scheduled':t,'expiry':stamp(expiry)-4*HOUR,'total':len(raw),
           'pre':list(before.values()),'post':sorted(after,key=lambda r:r[1])}
    temp=path.with_suffix('.part')
    temp.write_bytes(gzip.compress(json.dumps(saved,separators=(',',':')).encode(),mtime=0))
    temp.replace(path)

def dates():
    d=dt.date(2022,5,1)
    while d<=dt.date(2026,11,12):
        if d.weekday() in (0,2,3):yield d
        d+=dt.timedelta(days=1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--first',default='2022-05-01');p.add_argument('--last',default='2026-11-12')
    p.add_argument('--workers',type=int,default=3);a=p.parse_args()
    DIR.mkdir(exist_ok=True)
    jobs=[d for d in dates() if a.first<=d.isoformat()<=a.last and not (DIR/(d.isoformat()+'.json.gz')).exists()]
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        for i,_ in enumerate(pool.map(collect,jobs),1):
            if i%25==0 or i==len(jobs):print(i,'/',len(jobs),flush=True)
