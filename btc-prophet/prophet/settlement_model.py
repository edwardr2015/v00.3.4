"""Settlement-average probability for Polymarket US BTC Up/Down five-minute contracts.

The contract settles UP when the arithmetic mean of the 60 one-second BRTI values strictly before
the closing boundary is >= the same average before the opening boundary (Polymarket US crypto
FAQ). The existing Reference Gap formula treats the close as a single spot print. Here the close
is an average, approximated with Brownian motion:

  seconds_left > 60:   mean = current reference
                       var  = s1^2 * ((seconds_left - 60) + 60/3)
  seconds_left <= 60:  part of the averaging window is already fixed
                       mean = ((60 - s) * fixed_average + s * current) / 60
                       var  = s1^2 * (s / 60)^2 * s / 3

s1^2 is the per-second log variance: (vol_15s * VOL_SCALE)^2 / 15. VOL_SCALE = 1.5 was the best
walk-forward choice on the 94 recorded markets (research/feature_research.py); it is fixed here,
not refitted live. Research shadow only.
"""
import math

WINDOW = 60
VOL_SCALE = 1.5
VERSION = "settlement-average-v1"


def probability(reference, opening, vol_15s, seconds_left, window_values=(), *, vol_scale=VOL_SCALE):
    if not (reference and opening and reference > 0 and opening > 0) or vol_15s is None:
        return None
    s = max(float(seconds_left), 1e-3)
    s1 = max(float(vol_15s), 1e-6) * vol_scale / math.sqrt(15)
    if s > WINDOW:
        mean, var = reference, s1 ** 2 * ((s - WINDOW) + WINDOW / 3)
    else:
        values = [v for v in window_values if v and v > 0]
        fixed = sum(values) / len(values) if values else reference
        mean = ((WINDOW - s) * fixed + s * reference) / WINDOW
        var = s1 ** 2 * (s / WINDOW) ** 2 * s / 3
    z = math.log(mean / opening) / math.sqrt(max(var, 1e-14))
    return min(.99, max(.01, .5 * (1 + math.erf(z / math.sqrt(2)))))
