"""Original autonomous accounts, each sized against its own USDC equity."""
from bisect import bisect_right
from dataclasses import replace
from datetime import date

from bots.grid import choppy_week
from research import ANCHOR, BOOKS, CAPITAL, DAY, HOUR, configure, iso, moments, replay, signal, timestamp


def account(market, book, start, end, *, settings, gate_level):
    config = replace(settings, breakout='hold' if book == 'grid' else 'full',
                     hedge_band_btc=.3, hedge_fraction=.5)
    equity = CAPITAL
    state = {name: dict(start=CAPITAL, peak=CAPITAL, dd=0.) for name in BOOKS}
    history = [(timestamp(start), CAPITAL)]
    cycles = 0
    previous_end = 0
    column = BOOKS.index(book) + 1
    end_time = timestamp(end)
    for scheduled in moments(start, end):
        if ((scheduled - ANCHOR) // (7 * DAY)) % 2 or scheduled < previous_end:
            continue
        if scheduled // DAY * DAY + config.days * DAY + 8 * HOUR > end_time:
            continue
        candidate, _ = market.entry(scheduled, config)
        if not candidate:
            continue
        past = [market.funding[scheduled - i * DAY][0] for i in range(8)]
        eligible = choppy_week(past) if book == 'grid' else signal(past, candidate['premium']) >= gate_level
        if not eligible:
            continue
        entry = configure(candidate, config, equity)
        if entry is None:
            continue
        previous_end = entry['expiry']
        cycle = replay(market, entry, config, account=state, trace='none',
                       with_option=book != 'grid', benchmark=book == 'delta')
        history.extend((point[0], equity + point[column]) for point in cycle['capital_path'])
        equity += cycle['components'][book]['net']
        state[book]['start'] = equity
        cycles += 1

    stamps = [stamp for stamp, _ in history]
    yearly = {}
    opening = CAPITAL
    for stamp in range(timestamp(start) + DAY, end_time + 1, DAY):
        day = date.fromisoformat(iso(stamp)[:10])
        new_year = day.month == 1 and day.day == 1
        if new_year or stamp == end_time:
            nav = history[bisect_right(stamps, stamp) - 1][1]
            yearly[str(day.year - int(new_year))] = nav / opening - 1
            opening = nav
    return dict(cagr=(equity / CAPITAL) ** (365 * DAY / (end_time - timestamp(start))) - 1,
                max_dd=state[book]['dd'], n=cycles, annual=yearly)
