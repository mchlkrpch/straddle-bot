"""Causal replay of a long inverse BTC straddle and an inventory-limited perpetual grid."""
import argparse
from collections import deque
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import median, stdev

from market import (
    DAY, END, FIRST, HOUR, MINUTE, ROOT, SPLIT, YEAR,
    choose, greeks, iso, load_series, moments, read, timestamp,
)

from bots.grid import passive_order, boundary_reduction
from bots.delta_hedge import next_target
from bots.straddle_grid import implied_straddle

BP = 0.0001
LOT = 0.0001
TICK = 0.1
BOOKS = ('combo', 'straddle', 'grid', 'delta')
COSTS = ('option_fee', 'option_spread', 'delivery_fee', 'perp_fee', 'slippage', 'capital', 'conversion')
PARTS = ('option', 'delta', 'gamma', 'theta', 'vega', 'residual', 'perp', 'funding', *COSTS)
BREAKOUT_POLICIES = {'hold': 0, 'half': 50, 'full': 100}


@dataclass(frozen=True)
class Settings:
    days: int = 14
    step_bp: float = 50
    size: float = 0.5
    width: float = 1.0
    iv_gate: float = 0.
    option_half_spread: float = 0.025
    maker_bp: float = 1.5
    taker_bp: float = 3.5
    trade_through_bp: float = 2.0
    slippage_bp: float = 2.0
    margin_fraction: float = 0.10
    carry: float = 0.05
    cap_btc: float = 0.75
    hedge_band_btc: float = 0.05
    max_quote_age_hours: float = 6
    fx_rate: float = 0.0
    inventory_levels: int = 0
    hedge_fraction: float = 1.
    perp_lot: float = .0001
    perp_tick: float = .1
    capital: float = 150_000
    breakout: str = 'hold'


class Market:
    def __init__(self, data, start, end):
        self.data = data
        self.left = timestamp(start) - 7 * DAY
        self.right = timestamp(end)
        self.bars = load_series(data, 'bars', self.left, self.right)
        self.funding = load_series(data, 'funding', self.left, self.right)
        self.delivery = read(data / 'delivery.json.gz')
        self.marks = {}

    def entry(self, scheduled, settings):
        source = self.data / 'entry' / f'{scheduled}-{scheduled}.json.gz'
        if not source.is_file() or scheduled not in self.funding:
            return None, 'нет снимка'
        snapshots = read(source)
        trade = choose(scheduled, snapshots, self.funding[scheduled][0], settings.days)
        if trade is None:
            return None, 'нет свежей ATM-пары'
        if any(obs[0] < scheduled - settings.max_quote_age_hours * HOUR for obs in trade['observations']):
            return None, 'снимок ATM старше лимита'
        # One-minute publication buffer for the scheduled index sample.
        entry = scheduled + MINUTE
        prev = self.bars.get(entry - MINUTE)
        if prev is None:
            return None, 'нет последней свечи'
        past = [self.bars.get(t - MINUTE) for t in range(entry - 7 * DAY, entry + 1, 15 * MINUTE)]
        if any(bar is None for bar in past):
            return None, 'нет прогрева RV'
        rv7 = realized([bar[3] for bar in past], 7 * DAY)
        tau = (trade['expiry'] - entry) / YEAR
        index = trade['underlying']
        marks = [greeks(index, trade['strike'], tau, iv, i == 0)
                 for i, iv in enumerate(trade['iv'])]
        mid = sum(mark[0] for mark in marks)
        spread = sum(max(index * 0.00005, mark[0] * settings.option_half_spread) for mark in marks)
        option_fee = sum(min(index * 0.0003, (mark[0] + max(index * 0.00005,
                             mark[0] * settings.option_half_spread)) * 0.125) for mark in marks)
        h = round(index * settings.step_bp * BP / TICK) * TICK
        wings = min(500, math.floor(settings.width * (mid + spread) / h)) if h else 0
        center = round((prev[3] - trade['strike']) / h) if h else 0
        if wings < 1 or abs(center) >= wings:
            return None, 'нет места для сетки'
        gamma = sum(mark[2] for mark in marks)
        raw_size = min(settings.size * gamma * h, settings.cap_btc / (wings + abs(center)))
        quantity = math.floor(raw_size / LOT + 1e-9) * LOT
        premium = mid + spread
        effective_iv = implied_straddle(index, trade['strike'], tau, premium + option_fee)
        trade.update(t=entry, scheduled=scheduled, mid=mid, spread=spread, option_fee=option_fee,
                     premium=premium, gamma=gamma, delta=sum(mark[1] for mark in marks),
                     step=h, wings=wings, center=center, quantity=quantity, rv7=rv7,
                     effective_iv=effective_iv, f0=prev[3])
        return trade, ''

    def option_marks(self, entry, cutoff):
        result = []
        for name in entry['names']:
            stem = f"{entry['scheduled']}_{name}"
            path = self.data / 'marks' / (stem + '.json.gz')
            if not path.is_file():
                partials = sorted(p for p in (self.data / 'marks').glob(stem + '_*.json.gz')
                                  if int(p.name.rsplit('_', 1)[1].split('.')[0]) >= cutoff)
                if not partials:
                    raise ValueError(f'Нет кеша опционных сделок: {stem}. Запустите market.py.')
                path = partials[-1]
            if path not in self.marks:
                self.marks[path] = read(path)
            result.append(iter(item for item in self.marks[path] if item[0] < cutoff))
        return result


def realized(prices, period):
    squared = sum(math.log(b / a) ** 2 for a, b in zip(prices, prices[1:]))
    return math.sqrt(squared * YEAR / period) if period > 0 else 0.


class Perpetual:
    def __init__(self):
        self.position = 0.
        self.gross = 0.
        self.fee = 0.
        self.slippage = 0.
        self.funding = 0.
        self.capital = 0.
        self.exposure = 0.
        self.fills = 0
        self.turnover = 0.
        self.max_margin = 0.

    def order(self, quantity, fill, reference, fee_bp, slip_bp=0):
        self.position += quantity
        self.gross += quantity * (reference - fill)
        self.fee += abs(quantity) * fill * (1 + math.copysign(slip_bp * BP, quantity)) * fee_bp * BP
        self.slippage += abs(quantity) * fill * slip_bp * BP
        self.fills += 1
        self.turnover += abs(quantity) * fill

    def carry_minute(self, previous, current, funding, settings):
        self.gross += self.position * (current - previous)
        self.capital += abs(self.position) * previous * settings.margin_fraction * settings.carry * MINUTE / YEAR
        self.max_margin = max(self.max_margin, abs(self.position) * max(previous, current) * settings.margin_fraction)
        # Amount already held through this minute; apply the published completed-hour rate once.
        self.exposure += self.position / 60
        if funding is not None:
            index, rate = funding
            self.funding -= self.exposure * index * rate
            self.exposure = 0.

    def values(self):
        return dict(perp=self.gross, funding=self.funding, perp_fee=-self.fee,
                    slippage=-self.slippage, capital=-self.capital)

    def pnl(self):
        return self.gross + self.funding - self.fee - self.slippage - self.capital


def replay(market, entry, settings, *, cutoff=None, trace='hourly', benchmark=True, with_option=True, account=None):
    start, expiry, strike = entry['t'], entry['expiry'], entry['strike']
    units = entry.get('units', 1.)
    stop = expiry if cutoff is None else min(cutoff, expiry)
    complete = stop == expiry
    if complete and iso(expiry)[:10] not in market.delivery:
        raise ValueError(f'Нет официальной цены экспирации {iso(expiry)}')
    if any(t not in market.bars for t in range(start, stop, MINUTE)):
        raise ValueError(f'Пропущены минутные данные для цикла {iso(start)}')
    if any(t not in market.funding for t in range((start // HOUR + 1) * HOUR, stop + 1, HOUR)):
        raise ValueError(f'Пропущен фандинг для цикла {iso(start)}')

    hedge, grid = Perpetual(), Perpetual()
    ivs = list(entry['iv'])
    marks = market.option_marks(entry, stop) if with_option else [iter(()), iter(())]
    next_marks = [next(s, None) for s in marks]
    interval = entry['step']
    i = entry['center']
    wings = entry['wings']
    quantity = entry['quantity']
    low_bound, high_bound = strike - wings * interval, strike + wings * interval
    close_time = expiry - 30 * MINUTE
    close_percent = BREAKOUT_POLICIES[settings.breakout]
    old_f = entry['f0']
    old_s = entry['underlying']
    basis = old_f - old_s
    previous_values = [greeks(old_s, strike, (expiry - start) / YEAR, iv, leg == 0)
                       for leg, iv in enumerate(ivs)]
    value = entry['mid']
    attribution = dict.fromkeys(('delta', 'gamma', 'theta', 'vega', 'residual'), 0.)
    option_fee, spread = entry['option_fee'], entry['spread']
    option_carry = 0.
    delivery_fee = 0.
    delta = entry['delta']
    boundary = None
    pending_reduction = 0.
    ambiguous = 0
    no_trade_bars = 0
    target = None
    rv_samples = [old_f]
    path = []
    peaks = dict.fromkeys(BOOKS, 0.)
    troughs = dict.fromkeys(BOOKS, 0.)
    drawdowns = dict.fromkeys(BOOKS, 0.)
    capital_paths = []
    execution_log = []
    lots = deque()
    paired_grid = 0.

    def record(t, f, option, force=False):
        fx_cost = settings.fx_rate * (entry['premium'] + option_fee)
        if t == expiry:
            fx_cost += settings.fx_rate * max(0., option + entry['mid'] - delivery_fee)
        with_option_pnl = units * (option - option_fee - spread - delivery_fee - option_carry - fx_cost)
        grid_pnl = grid.pnl()
        values = (with_option_pnl + grid_pnl, with_option_pnl, grid_pnl,
                  with_option_pnl + hedge.pnl())
        for book, pnl in zip(BOOKS, values):
            if account is not None:
                state = account[book]
                equity = state['start'] + pnl
                state['peak'] = max(state['peak'], equity)
                state['dd'] = max(state['dd'], 1 - equity / state['peak'])
            peaks[book] = max(peaks[book], pnl)
            troughs[book] = min(troughs[book], pnl)
            drawdowns[book] = max(drawdowns[book], peaks[book] - pnl)
        if force or trace == 'minute' or (t % HOUR == 0 and trace == 'hourly'):
            path.append([t, f, grid.position, hedge.position, units * delta + grid.position, *values])
        if t % DAY == 0 or force:
            capital_paths.append([t, *values])

    def fill(t, book, dq, price, reference, passive):
        nonlocal paired_grid
        book.order(dq, price, reference, settings.maker_bp if passive else settings.taker_bp,
                   0. if passive else settings.slippage_bp)
        if book is grid and passive:
            remaining = dq
            while lots and lots[0][0] * remaining < -1e-12:
                outstanding, opened = lots.popleft()
                closed = math.copysign(min(abs(outstanding), abs(remaining)), outstanding)
                paired_grid += closed * (price - opened)
                outstanding -= closed
                remaining += closed
                if abs(outstanding) > 1e-10:
                    lots.appendleft((outstanding, opened))
            if abs(remaining) > 1e-10:
                lots.append((remaining, price))
        if trace == 'minute':
            execution_log.append([t, 'grid' if book is grid else 'delta', dq, price, passive])

    record(start, old_f, 0., True)
    for t in range(start, stop, MINUTE):
        next_t = t + MINUTE
        opening, high, low, final, volume = market.bars[t]
        funding = market.funding.get(next_t)
        # Changes executed at an observed tradable open occur before mark-to-close P&L.
        if volume > 0:
            if t >= close_time:
                for book in (grid, hedge):
                    if abs(book.position) > 1e-10:
                        fill(t, book, -book.position, opening, old_f, False)
                pending_reduction = 0.
            else:
                if pending_reduction:
                    fill(t, grid, pending_reduction, opening, old_f, False)
                    boundary['reduction'] = dict(time=t, open=opening, change=pending_reduction,
                                                 remaining=grid.position)
                    pending_reduction = 0.
                if benchmark and target is not None:
                    fill(t, hedge, target - hedge.position, opening, old_f, False)
                    target = None
        else:
            no_trade_bars += 1
        for book in (grid, hedge):
            book.carry_minute(old_f, final, funding, settings)

        if t < close_time and boundary is None:
            dq, price, both = passive_order(strike, i, interval, wings, quantity, old_f,
                                            high, low, volume, settings.trade_through_bp,
                                            grid.position, settings.inventory_levels)
            if both:
                ambiguous += 1
            elif dq:
                fill(next_t, grid, dq, price, final, True)
                i += -1 if dq > 0 else 1
        if t < close_time and boundary is None and (final < low_bound or final > high_bound):
            boundary = dict(time=next_t, price=final, quantity=grid.position, grid_pnl=grid.gross,
                            funding=grid.funding, reduction=None)
            pending_reduction = boundary_reduction(grid.position, close_percent)

        tau = (expiry - next_t) / YEAR
        old_tau = tau + MINUTE / YEAR
        # An hourly index observed at h changes the basis at h+1 minute.
        index_row = market.funding.get(t)
        if index_row is not None and t - MINUTE in market.bars:
            basis = market.bars[t - MINUTE][3] - index_row[0]
        spot = final - basis
        if next_t == expiry:
            spot = market.delivery[iso(expiry)[:10]]
        new_ivs = ivs.copy()
        for leg in range(2):
            while next_marks[leg] is not None and next_marks[leg][0] < next_t:
                new_ivs[leg] = next_marks[leg][1]
                next_marks[leg] = next(marks[leg], None)
        if with_option:
            ds = spot - old_s
            new_values = []
            for leg, previous in enumerate(previous_values):
                _, d, g, _, _ = previous
                call = leg == 0
                spot_value = greeks(spot, strike, old_tau, ivs[leg], call)[0]
                time_value = greeks(spot, strike, tau, ivs[leg], call)[0]
                new = greeks(spot, strike, tau, new_ivs[leg], call)
                attribution['delta'] += d * ds
                attribution['gamma'] += 0.5 * g * ds * ds
                attribution['residual'] += spot_value - previous[0] - d * ds - 0.5 * g * ds * ds
                attribution['theta'] += time_value - spot_value
                attribution['vega'] += new[0] - time_value
                new_values.append(new)
            value, delta = sum(v[0] for v in new_values), sum(v[1] for v in new_values)
            previous_values, old_s = new_values, spot
            ivs = new_ivs
            target = next_target(delta, units, hedge.position, settings.hedge_band_btc, settings.hedge_fraction, settings.perp_lot) if benchmark and next_t < close_time else None
            if next_t == expiry:
                delivery_fee = min(spot * 0.00015, value * 0.125)
            option_carry += (entry['premium'] + option_fee) * settings.carry * MINUTE / YEAR

        old_f = final
        if (next_t - start) % (15 * MINUTE) == 0 or next_t == stop:
            rv_samples.append(final)
        record(next_t, final, value - entry['mid'], next_t == stop)

    if complete and (abs(grid.position) > 1e-8 or abs(hedge.position) > 1e-8):
        raise ValueError(f'Позиция не закрылась до экспирации {iso(expiry)}')

    option = dict(option=value - entry['mid'], **attribution, option_fee=-option_fee,
                  option_spread=-spread, delivery_fee=-delivery_fee, capital=-option_carry)
    fx_cost = settings.fx_rate * (entry['premium'] + option_fee)
    if complete:
        fx_cost += settings.fx_rate * max(0., value - delivery_fee)
    option['conversion'] = -fx_cost
    components = {}
    for name, use_option, book in (('combo', True, grid), ('straddle', True, None),
                                   ('grid', False, grid), ('delta', True, hedge)):
        row = dict.fromkeys(PARTS, 0.)
        for part, amount in option.items():
            row[part] += units * amount if use_option else 0.
        if book:
            for part, amount in book.values().items():
                row[part] += amount
        row['net'] = sum(row[k] for k in ('option', 'perp', 'funding', *COSTS))
        components[name] = row
    if with_option and abs(sum(attribution.values()) - option['option']) > 1e-5:
        raise AssertionError('Option attribution must telescope')

    rv = realized(rv_samples, stop - start)
    displacement = abs(math.log(old_f / entry['f0']))
    walk = sum(abs(math.log(b / a)) for a, b in zip(rv_samples, rv_samples[1:]))
    max_move = max(abs(math.log(p / entry['f0'])) for p in rv_samples)
    efficiency = displacement / walk if walk else 0.
    regime = ('всплеск' if rv > 1.35 * entry['effective_iv'] else
              'тренд' if displacement > 0.75 * rv * math.sqrt((stop - start) / YEAR) else 'боковик')
    return dict(entry=entry, complete=complete, through=stop, components=components, path=path,
                capital_path=capital_paths, minute_drawdown=drawdowns, boundary=boundary,
                minute_peak=peaks, minute_trough=troughs,
                rv=rv, regime=regime, log_displacement=displacement, max_move=max_move,
                efficiency=efficiency, fills=grid.fills, hedge_fills=hedge.fills,
                grid_turnover=grid.turnover, hedge_turnover=hedge.turnover,
                grid_margin=grid.max_margin, hedge_margin=hedge.max_margin,
                paired_grid=paired_grid, inventory_grid=grid.gross - paired_grid,
                ambiguous_bars=ambiguous, no_trade_bars=no_trade_bars, executions=execution_log)


def dates(start, end, settings):
    for scheduled in moments(start, end):
        expiry = scheduled // DAY * DAY + settings.days * DAY + 8 * HOUR
        if expiry <= timestamp(end):
            yield scheduled


def run(market, settings, start, end, *, gate=True, trace='hourly', benchmark=True):
    rows, skips = [], []
    account = {b: dict(start=settings.capital, peak=settings.capital, dd=0.) for b in BOOKS}
    previous_expiry = 0
    for scheduled in dates(start, end, settings):
        if scheduled < previous_expiry:
            skips.append([scheduled, 'цикл открыт'])
            continue
        entry, error = market.entry(scheduled, settings)
        if entry is None:
            skips.append([scheduled, error])
        elif gate and settings.iv_gate > 0 and entry['rv7'] < settings.iv_gate * entry['effective_iv']:
            skips.append([scheduled, 'фильтр RV/IV'])
        else:
            cycle = replay(market, entry, settings, trace=trace, benchmark=benchmark, account=account)
            cycle['rolling_dd'] = {b: account[b]['dd'] for b in BOOKS}
            for b in BOOKS:
                account[b]['start'] += cycle['components'][b]['net']
            rows.append(cycle)
            previous_expiry = entry['expiry']
    return rows, skips


def metrics(rows, start, end, capital):
    left, right = timestamp(start) // DAY * DAY, timestamp(end) // DAY * DAY
    result = {}
    for i, name in enumerate(BOOKS):
        daily = dict.fromkeys(range(left + DAY, right + DAY, DAY), 0.)
        outcomes = []
        peak = 0.
        current = 0.
        dd = 0.
        for row in rows:
            net = row['components'][name]['net']
            outcomes.append(net)
            dd = max(dd, row['minute_drawdown'][name], peak - current - row['minute_trough'][name])
            peak = max(peak, current + row['minute_peak'][name])
            previous = 0.
            for t, *values in row['capital_path']:
                day = ((t + DAY - 1) // DAY) * DAY
                if day in daily:
                    daily[day] += values[i] - previous
                previous = values[i]
            current += net
        equity = capital
        rets = []
        for change in daily.values():
            rets.append(change / equity)
            equity += change
        sigma = stdev(rets) if len(rets) > 1 else 0.
        sharpe = math.sqrt(365) * sum(rets) / len(rets) / sigma if sigma else None
        tail = sorted(outcomes)[:math.ceil(len(outcomes) * .05)]
        result[name] = dict(pnl=current, return_pct=current / capital * 100, sharpe=sharpe,
                            drawdown=dd, dd_pct=(rows[-1]['rolling_dd'][name] * 100 if rows else 0.), dd_initial_pct=dd / capital * 100,
                            cycle_count=len(rows), win_rate=sum(x > 0 for x in outcomes) / len(rows) if rows else 0.,
                            worst_5_mean=sum(tail) / len(tail) if tail else None,
                            median=median(outcomes) if outcomes else None)
    return result


def summarize(market, settings, start, end, *, gate=True, trace='hourly', benchmark=True):
    rows, skips = run(market, settings, start, end, gate=gate, trace=trace, benchmark=benchmark)
    return dict(settings=asdict(settings), start=start, end=end, cycles=rows, skips=skips,
                metrics=metrics(rows, start, end, settings.capital))


def latest(market, settings, start, end):
    status = {'status': 'нет пятничного сигнала'}
    expire = 0
    for t in moments(start, end):
        if t < expire or t + MINUTE >= market.right:
            continue
        entry, error = market.entry(t, settings)
        if not entry:
            status = {'signal': iso(t), 'status': error}
        elif settings.iv_gate and entry['rv7'] < settings.iv_gate * entry['effective_iv']:
            status = {'signal': iso(t), 'status': 'вне позиции', 'rv7': entry['rv7'],
                      'iv': entry['effective_iv']}
        else:
            expire = entry['expiry']
            if expire > market.right:
                cycle = replay(market, entry, settings, cutoff=market.right, trace='hourly')
                return {'signal': iso(t), 'status': 'бумажный цикл', 'through': iso(cycle['through']),
                        'entry': entry, 'last_state': cycle['path'][-1], 'boundary': cycle['boundary'],
                        'components': cycle['components']}
            status = {'signal': iso(t), 'status': 'цикл завершён'}
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, default=ROOT / 'data')
    p.add_argument('--start', default=FIRST)
    p.add_argument('--end', default=END, help='Exclusive UTC date')
    p.add_argument('--step-bp', type=float, default=50)
    p.add_argument('--size', type=float, default=.5)
    p.add_argument('--width', type=float, default=1.)
    p.add_argument('--days', type=int, default=Settings.days, choices=(7, 14, 21, 28))
    p.add_argument('--gate', type=float, default=Settings.iv_gate)
    p.add_argument('--breakout', choices=BREAKOUT_POLICIES, default=Settings.breakout)
    p.add_argument('--latest', action='store_true')
    p.add_argument('--json', type=Path)
    a = p.parse_args()
    if a.step_bp <= 0 or a.size < 0 or a.width <= 0 or a.gate < 0 or timestamp(a.start) >= timestamp(a.end):
        p.error('Invalid parameters or period')
    settings = Settings(step_bp=a.step_bp, size=a.size, width=a.width, days=a.days, iv_gate=a.gate,
                        breakout=a.breakout)
    market = Market(a.data, a.start, a.end)
    result = latest(market, settings, a.start, a.end) if a.latest else summarize(market, settings, a.start, a.end)
    if a.json:
        a.json.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    print(json.dumps(result if a.latest else result['metrics'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
