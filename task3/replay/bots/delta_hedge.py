"""Buy the same call/put pair. Hedge both option deltas with a linear BTC perpetual."""
from .grid import LOT
from .straddle_grid import opening_orders  # Both option legs use the same entry as the main bot.

def next_target(delta_per_pair,pairs,inventory,band_per_pair=.05,hedge_fraction=1.,lot=LOT):
    """Absolute BTC target for the next tradable open; None leaves inventory unchanged."""
    option_delta=hedge_fraction*pairs*delta_per_pair
    if abs(option_delta+inventory)<band_per_pair*pairs*hedge_fraction:
        return None
    return round(-option_delta/lot)*lot
