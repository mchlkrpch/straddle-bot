"""Draw both the interactive result and GitHub figures with the same SVG renderer."""
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring


def utc(stamp, fmt):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime(fmt)


def number(value, places=0):
    return f'{value:,.{places}f}'.replace(',', ' ').replace('.', ',')


def cycle_chart(label, row, executions):
    entry = row['entry']
    start, end = entry['t'], entry['expiry']
    strike, step, wings = entry['strike'], entry['step'], entry['wings']
    levels = [strike + index * step for index in range(-wings, wings + 1)]
    stop = end - 30 * 60_000
    if row['boundary']:
        stop = min(stop, row['boundary']['time'])
    active = entry['quantity'] > 0
    prices = [(point[0], point[1]) for point in row['path']]
    positions = [(start, 0.)]
    position = 0.
    for stamp, book, change, _, _ in executions:
        if book == 'grid':
            positions.append((stamp, position))
            position = round(position + change, 10)
            positions.append((stamp, position))
    positions.append((end, position))

    left, right = 90, 942
    svg = Element('svg', {'xmlns': 'http://www.w3.org/2000/svg', 'viewBox': '0 0 970 445',
                          'role': 'img', 'aria-labelledby': 'title desc', 'font-family': 'Arial, sans-serif'})
    title = f"{label} цикл · {utc(start, '%d.%m.%Y')} · {number(row['net']):s} USDC"
    SubElement(svg, 'title', id='title').text = title
    SubElement(svg, 'desc', id='desc').text = 'Цена перпетуала и уровни сетки сверху, позиция сетки по исполнениям снизу. Общая ось времени UTC.'
    SubElement(svg, 'rect', width='970', height='445', fill='white')

    def text(x, y, content, size=13, anchor='start', color='#354842'):
        SubElement(svg, 'text', {'x': str(x), 'y': str(y), 'text-anchor': anchor,
                                 'font-size': str(size), 'fill': color}).text = content

    def line(x1, y1, x2, y2, color, width=1, dash=None):
        attrs = dict(x1=f'{x1:.2f}', y1=f'{y1:.2f}', x2=f'{x2:.2f}', y2=f'{y2:.2f}',
                     stroke=color)
        attrs['stroke-width'] = str(width)
        if dash:
            attrs['stroke-dasharray'] = dash
        SubElement(svg, 'line', attrs)

    def x(stamp):
        return left + (stamp - start) / (end - start) * (right - left)

    def panel(values, top, bottom, decimals):
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * .09, 10 ** -decimals)
        lo, hi = lo - pad, hi + pad
        def y(value):
            return bottom - (value - lo) / (hi - lo) * (bottom - top)
        for i in range(4):
            tick = lo + i * (hi - lo) / 3
            line(left, y(tick), right, y(tick), '#e0e6e2', .9)
            text(left - 9, y(tick) + 4, number(tick, decimals), 12, 'end')
        return y

    def trace(points, y, color):
        coords = ' '.join(f'{x(t):.2f},{y(v):.2f}' for t, v in points)
        SubElement(svg, 'polyline', {'points': coords, 'fill': 'none', 'stroke': color,
                                    'stroke-width': '1.8', 'stroke-linejoin': 'round'})

    text(18, 28, title, 20)
    text(18, 55, 'Цена BTC-перпетуала, USDC · часовые отметки', 14, color='#176657')
    grid_info = f"Страйк {number(strike)} · шаг {number(step, 1)} · уровней на сторону {wings}"
    text(right, 55, grid_info, 13, 'end')
    price_axis = panel([p for _, p in prices] + (levels if active else [strike]), 77, 243, 0)
    if active:
        for level in levels:
            line(left, price_axis(level), x(stop), price_axis(level), '#82928a', .9, '4 4')
    trace(prices, price_axis, '#176657')
    text(18, 280, 'Позиция грида, BTC · изменения по исполнениям', 14, color='#356798')
    position_axis = panel([p for _, p in positions], 297, 379, 4)
    line(left, position_axis(0), right, position_axis(0), '#a2aaa5', 1)
    trace(positions, position_axis, '#356798')
    for i, anchor in [(0, 'start'), (1, 'middle'), (2, 'end')]:
        stamp = start + (end - start) * i / 2
        text(x(stamp), 404, utc(stamp, '%d.%m %H:%M'), 12, anchor)
    note = ('Пунктир — уровни сетки до её остановки ' + utc(stop, '%d.%m %H:%M')
            if active else 'Размер заявки равен нулю. Сетка не выставляла ордера.')
    text(left, 430, note + ' · UTC', 12)
    return tostring(svg, encoding='unicode')
