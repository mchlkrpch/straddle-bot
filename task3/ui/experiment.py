from dataclasses import replace
from datetime import date
from statistics import median

from backtest import replay
from portfolios import account
from research import BASE, START, END, daily, stats, study
from .cycles import cycle_chart

DEFAULTS = dict(start=START, end=END, step='50', size='0.5', width='1', days='14', gate='1')


def parameters(query):
    values = DEFAULTS | query
    start = date.fromisoformat(values['start']).isoformat()
    end = date.fromisoformat(values['end']).isoformat()
    days = int(values['days'])
    step, size, width, gate = (float(values[k]) for k in ('step', 'size', 'width', 'gate'))
    if not START <= start < end <= END:
        raise ValueError(f'Выберите период внутри {START} — {END}. Конец должен быть позже начала.')
    if not (days in (7, 14, 21, 28) and 10 <= step <= 300 and 0 <= size <= 2
            and .2 <= width <= 2 and 0 <= gate <= 3):
        raise ValueError('Значения выходят за границы параметров формы.')
    if days != 14 and start < '2025-08-01':
        raise ValueError('Для сроков 7, 21 и 28 дней выберите начало с 01.08.2025.')
    return start, end, replace(BASE, step_bp=step, size=size, width=width, days=days), gate


def cases(market, rows, settings):
    if not rows:
        return []
    ordered = sorted(rows, key=lambda row: row['entry']['t'])
    middle = median(row['net'] for row in ordered)
    # A tie on distance to the median selects the earlier trade.
    typical = min(ordered, key=lambda row: round(abs(row['net'] - middle), 6))
    picked = [('best', 'Лучший', max(ordered, key=lambda row: row['net'])),
              ('worst', 'Худший', min(ordered, key=lambda row: row['net'])),
              ('typical', 'Типичный', typical)]
    charts = []
    for key, label, row in picked:
        detailed = replay(market, row['entry'], settings, trace='minute')
        charts.append(dict(key=key, label=label, net=row['net'], start=row['entry']['t'],
                           svg=cycle_chart(label, row, detailed['executions'])))
    return charts


def calculate(market, query, *, charts_only=False):
    start, end, settings, gate = parameters(query)
    result, rows = study(market, start, end, settings, gate=gate, detail=True)
    charts = cases(market, rows, settings)
    if charts_only:
        return charts
    result['books'] = stats(rows, result)
    components = {}
    for row in rows:
        for key, value in row['components']['combo'].items():
            components[key] = components.get(key, 0.) + value
    independent = {book: account(market, book, start, end, settings=settings, gate_level=gate)
                   for book in ('combo', 'grid', 'delta')}
    return dict(result=result, cases=charts, independent=independent, components=components,
                equity=list(daily(rows, start, end)))
