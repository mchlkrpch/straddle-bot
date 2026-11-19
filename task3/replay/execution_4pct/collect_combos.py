"""Collect public Deribit combination trades, Monday–Thursday 10:00–18:00 UTC."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,time,timedelta,timezone
import gzip
import json
from pathlib import Path
import time as clock
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parent
ARCHIVE = HERE/'combo_tape'
HOUR = 3600000

def download(start,stop):
    params = dict(currency='BTC',kind='option_combo',start_timestamp=start,end_timestamp=stop-1,
                  sorting='asc',count=1000,include_old='true')
    url = 'https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time?' + urlencode(params)
    for attempt in range(6):
        try:
            with urlopen(url,timeout=35) as stream:
                obj = json.load(stream)
            if 'result' not in obj:
                raise ValueError(str(obj.get('error')))
            break
        except (HTTPError,OSError,ValueError):
            if attempt==5:
                raise
            clock.sleep(1.6**attempt)
    page = obj['result']
    if page['has_more']:
        mid = (start+stop)//2
        if mid==start:
            raise ValueError('Saturated one-millisecond interval')
        a,n = download(start,mid)
        b,m = download(mid,stop)
        return a+b,n+m
    found = []
    for r in page['trades']:
        name = r['instrument_name']
        if not name.startswith('BTC-STRD-') or r.get('block_trade_id') or r.get('liquidation'):
            continue
        legs = r.get('legs',[])
        identifier = name.split('-')
        expected = ['BTC-'+identifier[2]+'-'+identifier[3]+'-'+side for side in ('C','P')]
        legs = sorted(legs,key=lambda x:x['instrument_name'])
        if len(legs)!=2 or [x['instrument_name'] for x in legs]!=expected:
            continue
        if any(x.get('block_trade_id') or x.get('liquidation') or x['direction']!=r['direction'] or
               abs(x['amount']-r['amount'])>1e-8 for x in legs):
            continue
        if min(r['price'],r['amount'],r['index_price'],*(x['mark_price'] for x in legs)) <= 0:
            continue
        found.append([r['timestamp'],name,r['direction'],r['price'],r['amount'],r['index_price'],
                      [x['price'] for x in legs],[x['mark_price'] for x in legs],str(r['trade_id'])])
    return found,len(page['trades'])

def collect(d):
    path=ARCHIVE/(d.isoformat()+'.json.gz')
    if path.exists():
        return
    start=int(datetime.combine(d,time(10),timezone.utc).timestamp()*1000)
    rows,n=download(start,start+8*HOUR)
    payload=dict(date=d.isoformat(),all_combo_trades=n,rows=sorted(rows))
    temp=path.with_suffix('.part')
    temp.write_bytes(gzip.compress(json.dumps(payload,separators=(',',':')).encode(),mtime=0))
    temp.replace(path)

if __name__=='__main__':
    ARCHIVE.mkdir(exist_ok=True)
    first,last=date(2022,5,2),date(2026,11,13)
    dates=[first+timedelta(days=i) for i in range((last-first).days+1)]
    with ThreadPoolExecutor(4) as pool:
        for i,_ in enumerate(pool.map(collect,[d for d in dates if d.weekday()<4]),1):
            if i%120==0:
                print(i,flush=True)
