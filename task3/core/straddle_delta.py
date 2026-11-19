"""Half-delta hedge on matched straddles, with a no-trade band."""
import math

def hedge_target(delta, matched, position, to_center):
    target = -.5 * matched * delta
    width = .15 * matched
    if abs(position - target) <= width:
        return position
    if to_center:
        return round(target / .001) * .001
    if position < target - width:
        return math.ceil((target - width) / .001 - 1e-10) * .001
    return math.floor((target + width) / .001 + 1e-10) * .001

def run(engine, start, end, **options):
    return engine.run_packages('delta', start, end, **options)
