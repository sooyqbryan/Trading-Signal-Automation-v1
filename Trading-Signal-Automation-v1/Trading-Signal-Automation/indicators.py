"""Moving-average maths — we compute the EMA ourselves rather than trusting taapi's
pre-computed one."""


def ema(closes, length):
    """Exponential Moving Average, aligned to the END of `closes`. Seed with the SMA of the
    first `length` closes, then roll: ema = close*k + prev*(1-k), k = 2/(length+1)."""
    if length <= 0:
        raise ValueError(f"EMA length must be positive, got {length}")
    if len(closes) < length:
        raise ValueError(
            f"Need at least {length} closes to compute EMA({length}), got {len(closes)}"
        )

    k = 2 / (length + 1)

    seed = sum(closes[:length]) / length          # stage 1: seed with a simple average
    values = [seed]

    previous = seed                                # stage 2: roll forward
    for close in closes[length:]:
        current = (close * k) + (previous * (1 - k))
        values.append(current)
        previous = current

    return values


def relationship(fast_value, slow_value):
    """Which side of the slow MA the fast MA is on: "above", "below", or None on an exact tie.
    None matters — treating a tie as a side would look like a crossover and fire a false alert."""
    if fast_value > slow_value:
        return "above"
    if fast_value < slow_value:
        return "below"
    return None
