"""Exact-decimal indicator calculators (Step 26). Pure state machines: no I/O, no clock.

Arithmetic runs in a local Decimal context with 50 significant digits and ROUND_HALF_EVEN.
Internal state is never rounded further; only presented values are rounded to 8 decimal
places (ROUND_HALF_EVEN) by the engine. A value that is not defined is returned as
None with a reason code; it is never replaced by zero.

EMA(n)          Seed = simple mean of the first n closes after a reset (ready at bar n).
                Then EMA_t = a * close_t + (1 - a) * EMA_(t-1), with a = 2 / (n + 1).
Wilder RSI(n)   Change d_t = close_t - close_(t-1); gain = max(d, 0); loss = max(-d, 0).
                First averages = simple means of the first n gains/losses (ready at bar
                n + 1 after a reset). Then avg_t = (avg_(t-1) * (n - 1) + x_t) / n.
                RSI = 100 - 100 / (1 + avg_gain / avg_loss).
                avg_loss = 0 and avg_gain > 0 -> 100 with reason `no_losses`.
                avg_loss = 0 and avg_gain = 0 -> unavailable, reason `flat_prices`.
Volume SMA(n)   Simple mean of the last n volumes (ready at bar n after a reset). An
                average of zero volumes is a real 0, not a substitute.
Session VWAP    Typical price tp = (high + low + close) / 3 (a bar-level approximation of
                traded prices). VWAP = sum(tp * volume) / sum(volume) over in-session bars
                of the current session. Unavailable outside the session window, while
                cumulative volume is zero, and (gap policy `reset`) after a gap inside the
                session until the next session starts.
"""

from collections import deque
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

PRECISION = 50
ZERO, ONE, TWO, THREE, HUNDRED = Decimal(0), Decimal(1), Decimal(2), Decimal(3), Decimal(100)


class _Calc:
    """Each calculator runs its arithmetic inside a 50-digit, half-even local context."""

    @staticmethod
    def _setup(context):
        context.prec = PRECISION
        context.rounding = ROUND_HALF_EVEN


class EMA(_Calc):
    kind = "ema"

    def __init__(self, period):
        self.period = period
        self.reset()

    def reset(self):
        self.count, self.seed, self.value = 0, ZERO, None

    def update(self, close):
        with localcontext() as context:
            self._setup(context)
            self.count += 1
            if self.count < self.period:
                self.seed += close
                return None, ["warming_up"]
            if self.count == self.period:
                self.value = (self.seed + close) / self.period
            else:
                alpha = TWO / (self.period + 1)
                self.value = alpha * close + (ONE - alpha) * self.value
            return self.value, []


class WilderRSI(_Calc):
    kind = "rsi"

    def __init__(self, period):
        self.period = period
        self.reset()

    def reset(self):
        self.count, self.previous = 0, None
        self.gain_sum = self.loss_sum = ZERO
        self.avg_gain = self.avg_loss = None

    def update(self, close):
        with localcontext() as context:
            self._setup(context)
            self.count += 1
            if self.previous is None:
                self.previous = close
                return None, ["warming_up"]
            change = close - self.previous
            self.previous = close
            gain, loss = max(change, ZERO), max(-change, ZERO)
            changes = self.count - 1
            n = self.period
            if changes < n:
                self.gain_sum += gain
                self.loss_sum += loss
                return None, ["warming_up"]
            if changes == n:
                self.avg_gain = (self.gain_sum + gain) / n
                self.avg_loss = (self.loss_sum + loss) / n
            else:
                self.avg_gain = (self.avg_gain * (n - 1) + gain) / n
                self.avg_loss = (self.avg_loss * (n - 1) + loss) / n
            if self.avg_loss == 0:
                if self.avg_gain == 0:
                    return None, ["flat_prices"]
                return HUNDRED, ["no_losses"]
            return HUNDRED - HUNDRED / (ONE + self.avg_gain / self.avg_loss), []


class VolumeSMA(_Calc):
    kind = "volume_sma"

    def __init__(self, period):
        self.period = period
        self.reset()

    def reset(self):
        self.window = deque(maxlen=self.period)
        self.count = 0

    def update(self, volume):
        with localcontext() as context:
            self._setup(context)
            self.count += 1
            self.window.append(volume)
            if len(self.window) < self.period:
                return None, ["warming_up"]
            return sum(self.window, ZERO) / self.period, []


class SessionVWAP(_Calc):
    kind = "vwap"

    def __init__(self):
        self.session = None
        self.reset()

    def reset(self):
        self.cum_pv = self.cum_v = ZERO
        self.count = 0
        self.broken = False
        self.gap_ignored = False

    def start_session(self, key):
        self.reset()
        self.session = key

    def update(self, high, low, close, volume):
        with localcontext() as context:
            self._setup(context)
            self.count += 1
            if self.broken:
                return None, ["session_gap"]
            typical = (high + low + close) / THREE
            self.cum_pv += typical * volume
            self.cum_v += volume
            reasons = ["gap_ignored"] if self.gap_ignored else []
            if self.count == 1:
                reasons = ["session_start"] + reasons
            if self.cum_v == 0:
                return None, ["zero_volume"] + reasons
            return self.cum_pv / self.cum_v, reasons
