"""Read-only Deribit downloads and point-in-time entry construction. Python 3.10+."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import math
from pathlib import Path
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
YEAR = 365 * DAY
PRODUCT = 'BTC_USDC-PERPETUAL'
START = '2025-07-25'
END = '2026-11-14'
SPLIT = '2026-04-10'
FIRST = '2025-08-01'
DURATIONS = (7, 14, 21, 28)
WINDOW = 6 * HOUR
NORMALIZER = math.sqrt(2.0)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='seconds')


def option_expiry(symbol):
    date = datetime.strptime(symbol.split('-')[1], '%d%b%y').replace(hour=8, tzinfo=timezone.utc)
    return int(date.timestamp() * 1000)


def read(path):
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.tmp')
    partial.write_bytes(gzip.compress(json.dumps(data, separators=(',', ':'), allow_nan=False).encode(),
                                       compresslevel=5, mtime=0))
    partial.replace(path)


def request(method, history=False, **params):
    host = 'history' if history else 'www'
    url = f'https://{host}.deribit.com/api/v2/public/{method}?' + urlencode(params)
    for retry in range(5):
        try:
            raw = subprocess.check_output(['curl', '-fsS', '--max-time', '25', url])
            response = json.loads(raw)
            if 'result' not in response:
                raise ValueError(response.get('error'))
            return response['result']
        except (ValueError, subprocess.CalledProcessError) as error:
            if retry == 4:
                raise RuntimeError(url) from error
            time.sleep(1.5 ** retry)


def get_trades(left, right, symbol=None):
    # Split saturated intervals, including timestamp ties at page boundaries.
    params = dict(start_timestamp=left, end_timestamp=right - 1, sorting='asc',
                  count=10000, include_old='true')
    if symbol:
        method = 'get_last_trades_by_instrument_and_time'
        params['instrument_name'] = symbol
    else:
        method = 'get_last_trades_by_currency_and_time'
        params.update(currency='BTC', kind='option')
    page = request(method, True, **params)
    if page['has_more']:
        middle = (left + right) // 2
        if not left < middle < right:
            raise RuntimeError(f'Trade page saturated within one millisecond: {left}')
        return get_trades(left, middle, symbol) + get_trades(middle, right, symbol)
    return [item for item in page['trades'] if left <= item['timestamp'] < right]


def moments(first, end):
    left = timestamp(first) // DAY * DAY
    while datetime.fromtimestamp(left / 1000, timezone.utc).weekday() != 4:
        left += DAY
    while left + 12 * HOUR < timestamp(end):
        yield left + 12 * HOUR
        left += 7 * DAY


def cdf(x):
    return 0.5 * (1 + math.erf(x / NORMALIZER))


def greeks(spot, strike, tau, iv, call):
    if tau <= 0:
        return max((spot - strike) * (1 if call else -1), 0), 0., 0., 0., 0.
    root = math.sqrt(tau)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * tau) / (iv * root)
    d2 = d1 - iv * root
    n = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    delta = cdf(d1) if call else cdf(d1) - 1
    value = spot * delta - strike * (cdf(d2) if call else cdf(d2) - 1)
    gamma = n / (spot * iv * root)
    theta = -0.5 * spot * n * iv / root
    vega = spot * n * root
    return value, delta, gamma, theta, vega


def pack_trade(item, expiry=None):
    if expiry is None:
        expiry = option_expiry(item['instrument_name'])
    spot = item['index_price']
    return [item['timestamp'], item['mark_price'] * spot, spot, item['price'] * spot,
            item['direction'], expiry, item.get('iv', 0), item['trade_id']]


def mark_iv(trade, strike, call):
    t, mark, spot, _, _, expiry, fallback, _ = trade
    tau = (expiry - t) / YEAR
    if tau <= 0:
        return None
    lo, hi = 0.03, 3.0
    if greeks(spot, strike, tau, lo, call)[0] > mark or greeks(spot, strike, tau, hi, call)[0] < mark:
        return None
    for _ in range(26):
        middle = (lo + hi) * 0.5
        if greeks(spot, strike, tau, middle, call)[0] < mark:
            lo = middle
        else:
            hi = middle
    return (lo + hi) * 0.5


def choose(t, snapshots, spot, days):
    expiry = t // DAY * DAY + days * DAY + 8 * HOUR
    pairs = {}
    for name, trade in snapshots.items():
        if trade[5] != expiry or not t - WINDOW <= trade[0] < t:
            continue
        _, _, level, side = name.split('-')
        strike = float(level)
        if abs(strike / spot - 1) > 0.03:
            continue
        pairs.setdefault(strike, {})[side] = (name, trade)
    for strike in sorted(pairs, key=lambda k: (abs(k - spot), k)):
        pair = pairs[strike]
        if set(pair) != {'C', 'P'}:
            continue
        vols = [mark_iv(pair[side][1], strike, side == 'C') for side in ('C', 'P')]
        if all(vol is not None for vol in vols):
            return dict(t=t, expiry=expiry, strike=strike, underlying=spot, iv=vols,
                        names=[pair[s][0] for s in ('C', 'P')],
                        observations=[pair[s][1] for s in ('C', 'P')])
    return None


def scan(t):
    snapshots = {}
    expiry_cache = {}
    for item in get_trades(t - WINDOW, t):
        name = item['instrument_name']
        if not name.startswith('BTC-') or item.get('block_trade_id') or item.get('combo_id'):
            continue
        if name not in expiry_cache:
            expiry_cache[name] = option_expiry(name)
        expiry = expiry_cache[name]
        days = (expiry // DAY) - (t // DAY)
        if days not in DURATIONS:
            continue
        trade = pack_trade(item, expiry)
        if name not in snapshots or snapshots[name][0] <= trade[0]:
            snapshots[name] = trade
    return snapshots


def get_market(data, first, end, workers):
    left, right = timestamp(first), timestamp(end)
    # Chunk by 3 days because chart endpoint silently truncates at about 5000 bars.
    jobs = []
    for t in range(left, right, 3 * DAY):
        jobs.append(('bars', t, min(right, t + 3 * DAY)))
    for t in range(left, right, 28 * DAY):
        jobs.append(('funding', t, min(right, t + 28 * DAY)))
    for t in moments(first, end):
        jobs.append(('entry', t, t))
    jobs = [j for kind in ('entry', 'bars', 'funding') for j in jobs if j[0] == kind]

    def collect(job):
        kind, start, stop = job
        path = data / kind / f'{start}-{stop}.json.gz'
        if path.exists():
            return
        if kind == 'entry':
            result = scan(start)
        elif kind == 'bars':
            chart = request('get_tradingview_chart_data', instrument_name=PRODUCT,
                            start_timestamp=start, end_timestamp=stop - 1, resolution=1)
            if chart.get('status') != 'ok':
                raise ValueError(f'No chart data {iso(start)}')
            result = list(zip(*(chart[k] for k in ('ticks','open','high','low','close','volume'))))
            if {row[0] for row in result} != set(range(start, stop, MINUTE)):
                raise ValueError(f'Incomplete chart {iso(start)}')
        else:
            page = request('get_funding_rate_history', instrument_name=PRODUCT,
                           start_timestamp=start, end_timestamp=stop)
            result = [[x['timestamp'], x['index_price'], x['interest_1h']] for x in page
                      if start < x['timestamp'] <= stop]
        write(path, result)

    with ThreadPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(collect, jobs), 1):
            if i % 20 == 0 or i == len(jobs):
                print(f'Market files {i}/{len(jobs)}', flush=True)

    path = data / 'delivery.json.gz'
    deliveries = read(path) if path.exists() else {}
    if not all(iso(t)[:10] in deliveries for t in range(left, right, DAY)):
        offset = 0
        while True:
            page = request('get_delivery_prices', index_name='btc_usdc', count=100, offset=offset)
            if not page['data']:
                break
            deliveries.update((row['date'], row['delivery_price']) for row in page['data'])
            offset += len(page['data'])
            if min(deliveries) <= first or offset >= page['records_total']:
                break
        write(path, deliveries)


def load_series(data, kind, start, end):
    series = {}
    for path in (data / kind).glob('*.json.gz'):
        left, right = map(int, path.name.split('.')[0].split('-'))
        if left > end or right < start:
            continue
        for row in read(path):
            if start <= row[0] <= end:
                series[row[0]] = row[1:]
    return series


def get_option_paths(data, first, end, workers, live=False):
    start, stop = timestamp(first), timestamp(end)
    funding = load_series(data, 'funding', start, stop)
    jobs = {}
    for t in moments(first, end):
        if t not in funding:
            continue
        snapshots = read(data / 'entry' / f'{t}-{t}.json.gz')
        for days in DURATIONS:
            entry = choose(t, snapshots, funding[t][0], days)
            if entry is None or (entry['expiry'] > stop and not live):
                continue
            for name in entry['names']:
                jobs[(t, name)] = entry['expiry']

    def collect(job):
        (t, name), expiry = job
        right = min(expiry, stop)
        suffix = '' if right == expiry else f'_{right}'
        path = data / 'marks' / f'{t}_{name}{suffix}.json.gz'
        if path.exists():
            return
        trades = (p for p in get_trades(t, right, name)
                  if not p.get('block_trade_id') and not p.get('combo_id'))
        _, _, strike, side = name.split('-')
        strike = float(strike)
        marks = []
        for item in trades:
            trade = pack_trade(item, expiry)
            iv = mark_iv(trade, strike, side == 'C')
            if iv is not None:
                marks.append([trade[0], iv, trade[2]])
        write(path, marks)

    with ThreadPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(collect, jobs.items()), 1):
            if i % 50 == 0 or i == len(jobs):
                print(f'Option paths {i}/{len(jobs)}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--start', default=START)
    parser.add_argument('--end', default=END, help='Exclusive UTC date')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--only', choices=('market', 'marks'))
    parser.add_argument('--paper', action='store_true', help='Include open-cycle marks through --end')
    arguments = parser.parse_args()
    if arguments.only != 'marks':
        get_market(arguments.data, arguments.start, arguments.end, arguments.workers)
    if arguments.only != 'market':
        get_option_paths(arguments.data, arguments.start, arguments.end, arguments.workers, arguments.paper)


if __name__ == '__main__':
    main()
