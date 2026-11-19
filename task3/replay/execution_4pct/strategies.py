"""Trading decisions. Prices in BTC for options and USDC for the linear perpetual."""
import math
from dataclasses import dataclass

def floor_lot(value, lot):
    return math.floor(max(0., value) / lot + 1e-10) * lot

def tick_btc(price):
    return .0001 if price <= .005 + 1e-12 else .0005

def option_bid(mark):
    limit = mark - max(.00005, .025 * mark)
    return floor_lot(limit, tick_btc(limit))

def qualifying_volume(row, price, side, participation):
    _, _, _, _, printed, aggressor, size, _ = row
    tick = tick_btc(printed if side == "sell" else price)
    through = price - printed if side == "sell" else printed - price
    if aggressor != side or through + 1e-12 < tick:
        return 0.
    return floor_lot(size * participation, .1)

def option_fee(price, index, delivery=False):
    return min((.00015 if delivery else .0003) * index, .125 * price)

@dataclass(frozen=True)
class TradingCosts:
    participation: float = .25
    through: float = .0002
    impact_bp: float = 2.
    fx: float = .001
    maker_bp: float = 1.5
    taker_bp: float = 5.
    carry: float = .05
    margin: float = .10

STRESS = TradingCosts(participation=.10, through=.0005, impact_bp=5., fx=.002)
