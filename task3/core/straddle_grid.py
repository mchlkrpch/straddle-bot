"""Inventory risk budget of the grid attached to a filled native BTC straddle."""
import math
from strategies import floor_lot

def grid_unit(nav, perp_price, matched, gamma, halfwidth):
    """Order quantity per level, with gamma, capital and three-level inventory caps."""
    capital_cap = .10 * nav / perp_price / 3
    pair_cap = .75 * matched / 3
    gamma_size = .5 * matched * gamma * halfwidth
    return floor_lot(min(capital_cap, pair_cap, gamma_size), .001)



def inventory_quotes(anchor, halfwidth, position, unit, skew, previous):
    lots = round(position / unit)
    center = anchor - skew * lots * halfwidth
    bid = math.floor(center - halfwidth)
    ask = math.ceil(center + halfwidth)
    return (bid if lots < 3 and bid < previous else None,
            ask if lots > -3 and ask > previous else None)

def passive_grid_fill(quotes, high, low, volume, unit, costs):
    if unit > costs.participation * volume + 1e-12:
        return None
    bid, ask = quotes
    down = bid is not None and low < bid * (1 - costs.through)
    up = ask is not None and high > ask * (1 + costs.through)
    if down == up:
        return None
    return (unit, bid) if down else (-unit, ask)



def run(engine, start, end, **options):
    return engine.run_packages('combo', start, end, **options)
