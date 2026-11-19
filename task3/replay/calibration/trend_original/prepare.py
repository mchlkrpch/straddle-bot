"""Convert the local Deribit and OKX caches into uniform public-market inputs."""
import argparse
import gzip
import json
from pathlib import Path

STEP = 300_000
HOUR = 3_600_000
DAY = 24 * HOUR


def read(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    path.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))


def deribit(folder):
    minutes = [row for p in sorted((folder/'bars').glob('*.json.gz')) for row in read(p)]
    bars = []
    for first in range(0, len(minutes), 5):
        group = minutes[first:first+5]
        t = group[0][0]
        assert t % STEP == 0 and [r[0] for r in group] == list(range(t,t+STEP,60_000))
        # Retain the first traded open. A carry-forward bar has no executable open.
        trade = next((r for r in group if r[-1]>0), None)
        bars.append([t, trade[1] if trade else None, group[-1][4]])
    funding = [row for p in sorted((folder/'funding').glob('*.json.gz')) for row in read(p)]
    return dict(venue='deribit', instrument='BTC_USDC-PERPETUAL', fee_bps=3.5,
                funding_mode='hourly_average', bars=bars, funding=funding)


def okx(folder):
    import pandas as pd

    bars = pd.read_parquet(folder/'swap_5m.parquet', columns=['open_ms','open','close'])
    rows = [[r.open_ms,r.open,r.close] for r in bars.itertuples(index=False)]
    reference = json.loads((folder/'reference.json').read_text())
    funding = [[int(t),None,rate] for t,rate in reference['funding'].items()]
    return dict(venue='okx', instrument='BTC-USDT-SWAP', fee_bps=5.0,
                funding_mode='point', bars=rows, funding=funding)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('venue', choices=('deribit','okx'))
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    write(a.output, (deribit if a.venue=='deribit' else okx)(a.source))
