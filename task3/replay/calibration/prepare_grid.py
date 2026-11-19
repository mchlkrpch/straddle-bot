"""Write little-endian double rows: OHLCV, preceding causal fair, funding per BTC/min."""
from array import array
import gzip
import json
from pathlib import Path
import sys
from reference import ARCHIVE, MINUTE, timestamp

def prepare(output):
    base=Path(__file__).resolve().parents[1]/'data'
    first,last=timestamp('2022-04-24'),timestamp('2026-11-14')
    funding={}
    for f in (base/'funding').glob('*.gz'):
        with gzip.open(f,'rt') as z:
            for t,idx,rate in json.load(z):
                funding[t]=idx*rate/60
    def linear():
        for f in sorted((base/'bars').glob('*.gz')):
            with gzip.open(f,'rt') as z:
                for r in json.load(z):
                    if first<=r[0]<last:
                        yield r
    market=iter(linear())
    ready=0
    avg=None
    prev=None
    expected=first
    with open(output,'wb') as out:
        for f in sorted(ARCHIVE.glob('*.gz')):
            start=int(f.name.split('.')[0])
            with gzip.open(f,'rt') as z:
                rows=json.load(z)
            buf=array('d')
            for j,ref in enumerate(rows):
                t=start+j*MINUTE
                at,op,high,low,close,volume=next(market)
                assert t==at==expected
                fair=prev[0]*avg if prev and prev[1] and ready>=60 else 0.
                buf.extend((op,high,low,close,volume,fair,funding[(t//3600000+1)*3600000]))
                if volume>0 and ref and ref[1]:
                    ratio=close/ref[0]
                    avg=ratio if not ready else avg+(ratio-avg)/60
                    ready+=1
                prev=ref
                expected+=MINUTE
            if sys.byteorder!='little':
                buf.byteswap()
            buf.tofile(out)
    assert expected==last

if __name__=='__main__':
    prepare(sys.argv[1])
