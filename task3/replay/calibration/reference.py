"""Download liquid inverse BTC perpetual minute closes for a causal pricing reference."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT/'reference'
MINUTE = 60000
DAY = 1440*MINUTE

def timestamp(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()*1000)

def get_chunk(span):
    lo,hi=span
    path=ARCHIVE/f'{lo}.json.gz'
    if path.exists():
        return
    query=urlencode(dict(instrument_name='BTC-PERPETUAL',resolution='1',
                         start_timestamp=lo,end_timestamp=hi-1))
    url='https://www.deribit.com/api/v2/public/get_tradingview_chart_data?'+query
    for retry in range(6):
        try:
            with urlopen(url,timeout=35) as f:
                data=json.load(f)['result']
            ticks=data['ticks']
            by_time={t:[c, int(v>0)] for t,c,v in zip(ticks,data['close'],data['volume'])}
            rows=[by_time.get(t) for t in range(lo,hi,MINUTE)]
            if not any(rows):
                raise ValueError('Empty inverse perpetual candles')
            break
        except (OSError,ValueError,KeyError):
            if retry==5:
                raise
            time.sleep(1.7**retry)
    tmp=path.with_suffix('.part')
    tmp.write_bytes(gzip.compress(json.dumps(rows,separators=(',',':')).encode(),mtime=0))
    tmp.replace(path)

if __name__=='__main__':
    ARCHIVE.mkdir(exist_ok=True)
    first,last=timestamp('2022-04-24'),timestamp('2026-11-14')
    spans=[(t,min(last,t+3*DAY)) for t in range(first,last,3*DAY)]
    with ThreadPoolExecutor(4) as pool:
        for i,_ in enumerate(pool.map(get_chunk,spans),1):
            if i%100==0:
                print(i,flush=True)
