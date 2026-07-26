"""Entry point — the signal loop.

    Run one cycle:    python main.py
    Run continuously: python main.py --loop

One cycle is IDEMPOTENT: a bar we have already processed is skipped, which is what makes
overlapping runs, restarts and manual re-runs safe.
"""
import argparse
import logging
import sys
import time

import config
import market_data
import notifier
import state
from indicators import ema, relationship

log = logging.getLogger("signal")

# How long one bar lasts, in seconds. Used to detect gaps (missed bars).
TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# Wake this long AFTER a bar closes rather than exactly on it. The exchange needs a
# moment to finalise and publish the bar, and our clock is not their clock. 20s is
# cheap insurance against polling a bar that is not served yet.
BAR_CLOSE_OFFSET_SECONDS = 20

# --- Data-outage alerting ----------------------------------------------------
# A single failed fetch is a non-event: we poll several times a bar, so one blip
# is covered by the next poll minutes later. We only tell the user once we have
# failed OUTAGE_ALERT_AFTER cycles BACK TO BACK - roughly one full bar's worth of
# blindness at a 5-minute interval - because that is when a real signal could
# actually be at risk. A "failed cycle" here means the whole source chain died:
# both taapi keys AND the Binance fallback. If any of them answered, data flowed
# and the counter resets.

OUTAGE_ALERT_AFTER = 3
_consecutive_failures = {}   # instrument name -> back-to-back failed cycles
_in_outage = {}             # instrument name -> have we already sent the outage alert?


def _note_fetch_failure(name, interval):
    """One cycle failed for this instrument. Alert ONCE when we cross the threshold."""
    _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
    n = _consecutive_failures[name]
    if n >= OUTAGE_ALERT_AFTER and not _in_outage.get(name):
        _in_outage[name] = True
        minutes = round(n * interval / 60)
        log.error("[%s] DATA OUTAGE: %d consecutive failed cycles (~%d min). "
                  "Alerting once; the service stays up and keeps retrying.",
                  name, n, minutes)
        # Informational only - a failed notice must never veto anything, so unlike
        # a signal we do not check the return value.
        notifier.send(notifier.format_data_outage(name, n, minutes))


def _note_fetch_success(name, interval):
    """A cycle succeeded. If we were in an outage, announce recovery and reset."""
    if _in_outage.get(name):
        n = _consecutive_failures.get(name, 0)
        minutes = round(n * interval / 60)
        log.info("[%s] DATA RECOVERED after %d failed cycles (~%d min).", name, n, minutes)
        notifier.send(notifier.format_data_recovered(name, n, minutes))
    _consecutive_failures[name] = 0
    _in_outage[name] = False


def sleep_until_next_cycle(interval):
    """Sleep until the next WALL-CLOCK slot, rather than `sleep(interval)` after the work."""
    now = time.time()
    next_wake = (((now - BAR_CLOSE_OFFSET_SECONDS) // interval + 1) * interval
                 + BAR_CLOSE_OFFSET_SECONDS)
    time.sleep(next_wake - now)


def setup_logging():
    """
    Log to BOTH the console and a file.

    The file is the evidence for the >=3-day unattended run - it has to show what
    the service did at every step, including the boring "nothing happened" cycles.
    A log that only records alerts cannot prove the service was alive between them.
    """
    fmt = logging.Formatter(
        "%(asctime)sZ | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt.converter = time.gmtime  # log in UTC -- the same clock the exchange uses

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler("signals.log")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def process_instrument(instrument, current_state):
    """
    Run the core mechanism for ONE instrument.

    Returns True if state should be saved. The caller saves - this function never
    saves on its own, so a failed alert can veto the save and force a retry.
    """
    name = instrument["name"]
    last_bar_time, last_rel, last_exchange, last_source = state.get_instrument_state(
        current_state, name
    )

    candles, source, exchange_id = market_data.fetch_closed_candles(
        instrument, config.TIMEFRAME
    )

    # --- Tell the user if the data source changed (ONCE, on the transition) ----
    # Deduped against last_source: an outage lasting hours must not send hundreds of identical messages.
    if last_source is not None and source != last_source:
        is_fallback = source != "taapi"
        log.warning("[%s] Data source changed: %s -> %s", name, last_source, source)
        notifier.send(notifier.format_source_change(
            name, last_source, source, exchange_id, is_fallback
        ))

    newest = candles[-1]
    bar_time = newest["timestamp"]
    bar_human = newest.get("timestampHuman", str(bar_time))

    # --- Guard 1: have we already handled this bar? -------------------------
    # This single check stops duplicate alerts makes restarts safe and makes two overlapping runs harmless.
    if last_bar_time is not None and bar_time <= last_bar_time:
        log.info("[%s] No new closed bar (newest %s already processed). Nothing to do.",
                 name, bar_human)
        return False

    # --- Guard 2: did we miss bars while we were down? ----------------------
    # Policy: SKIP THE MISSED BARS, LOG THE GAP, AND STILL ALERT ONCE on the
    # current relationship if it changed while we were away.
    bar_seconds = TIMEFRAME_SECONDS.get(config.TIMEFRAME)
    gap_bars = 0
    if last_bar_time is not None and bar_seconds:
        gap_bars = (bar_time - last_bar_time) // bar_seconds
        if gap_bars > 1:
            log.warning(
                "[%s] GAP DETECTED: %d bars passed since our last processed bar "
                "(%s). Skipping to the current bar without replaying old crossovers.",
                name, gap_bars, last_bar_time,
            )

    # --- Compute the two EMAs ----------------------------------------------
    closes = [c["close"] for c in candles]
    fast_series = ema(closes, config.FAST_MA)
    slow_series = ema(closes, config.SLOW_MA)

    # The series are different lengths (each is shortened by its own seed), but
    # both are aligned to the END of `closes`, so [-1] is the same bar in both.
    fast_value = fast_series[-1]
    slow_value = slow_series[-1]

    current_rel = relationship(fast_value, slow_value)

    # --- Guard 3: an exact tie is not a crossover ---------------------------
    if current_rel is None:
        log.info("[%s] EMA %d and EMA %d are exactly equal on bar %s -- no side, "
                 "holding previous state (%s).",
                 name, config.FAST_MA, config.SLOW_MA, bar_human, last_rel)
        state.set_instrument_state(current_state, name, bar_time, last_rel,
                                   last_exchange, source)
        return True

    # --- Guard 4: first ever run establishes a baseline, it does not alert --
    # On the very first run we have no previous relationship, so we cannot know a
    # cross happened -- the fast EMA has simply always been on this side as far as
    # we know. Alerting here would be inventing a signal.
    if last_rel is None:
        log.info("[%s] BASELINE established on bar %s: fast EMA is %s slow EMA "
                 "(fast=%.2f slow=%.2f). No alert -- nothing crossed, this is our "
                 "first observation.",
                 name, bar_human, current_rel, fast_value, slow_value)
        state.set_instrument_state(current_state, name, bar_time, current_rel,
                                   exchange_id, source)
        return True

    # --- Guard 5: did we cross an EXCHANGE boundary? ------------------------
    # Our saved relationship was computed from one exchange's prices. Another
    # exchange quotes slightly different prices, so comparing across the boundary
    # could report a crossover that never happened -- a false signal, which is
    # worse than a missed one.
    #
    # So we treat an exchange change like a first run: re-baseline, no alert. We
    # lose crossover detection for exactly one bar, and say so out loud.
    #
    # NOTE this does NOT fire for our taapi -> binance.vision failover, because
    # both read the SAME exchange (both exchange_id == "binance"). That failover is
    # verified safe: identical closed bars, identical EMAs. This guard exists so
    # that adding a genuinely different exchange later stays a safe config change.
    if last_exchange is not None and exchange_id != last_exchange:
        log.warning(
            "[%s] EXCHANGE CHANGED (%s -> %s) on bar %s. Re-baselining instead of "
            "comparing across a price discontinuity: fast EMA is now %s slow EMA. "
            "Any crossover on this bar was suppressed on purpose.",
            name, last_exchange, exchange_id, bar_human, current_rel,
        )
        notifier.send(notifier.format_rebaseline(
            name, last_exchange, exchange_id, current_rel, bar_human
        ))
        state.set_instrument_state(current_state, name, bar_time, current_rel,
                                   exchange_id, source)
        return True

    # --- The signal ---------------------------------------------------------
    if current_rel != last_rel:
        side = "BUY" if current_rel == "above" else "SELL"
        log.info("[%s] *** CROSSOVER on bar %s: fast went %s -> %s = %s signal *** "
                 "(source: %s, fast=%.2f slow=%.2f)",
                 name, bar_human, last_rel, current_rel, side, source,
                 fast_value, slow_value)

        message = notifier.format_signal(
            name=name, side=side, bar_time=bar_time,
            bar_seconds=TIMEFRAME_SECONDS[config.TIMEFRAME],
            price=newest["close"], fast_value=fast_value, slow_value=slow_value,
            previous_rel=last_rel, fast_len=config.FAST_MA,
            slow_len=config.SLOW_MA, timeframe=config.TIMEFRAME,
            source=source, exchange_id=exchange_id, gap_bars=gap_bars,
        )

        # ORDER MATTERS: only record this bar as done once delivery is CONFIRMED.
        if not notifier.send(message):
            log.error("[%s] Alert delivery failed -- NOT saving state so the next "
                      "run retries this bar.", name)
            return False

        state.set_instrument_state(current_state, name, bar_time, current_rel,
                                   exchange_id, source)
        return True

    # --- No crossover -------------------------------------------------------
    log.info("[%s] New bar %s processed: fast EMA still %s slow EMA "
             "(fast=%.2f slow=%.2f). No crossover.",
             name, bar_human, current_rel, fast_value, slow_value)
    state.set_instrument_state(current_state, name, bar_time, current_rel,
                               exchange_id, source)
    return True


def run_once(interval=300):
    """
    Run one full cycle across every configured instrument.

    `interval` is only used to phrase the outage/recovery alerts ("~15 min");
    it defaults to the production value so a single-cycle run still reads sensibly.
    """
    current_state = state.load_state()
    should_save = False

    for i, instrument in enumerate(config.INSTRUMENTS):
        name = instrument["name"]
        try:
            if process_instrument(instrument, current_state):
                should_save = True
            # Reaching here means the fetch chain answered (data flowed), whether
            # or not there was a new bar to act on -- a healthy cycle. This also
            # closes out any outage we were in.
            _note_fetch_success(name, interval)
        except market_data.AllSourcesFailed:
            # A genuine data outage: every source failed to return candles. THIS is
            # what the outage alert is for, so count it toward the threshold.
            log.exception("[%s] All data sources failed this cycle. Continuing.", name)
            _note_fetch_failure(name, interval)
        except Exception:
            # A non-data error (a bug, a malformed response we did not anticipate).
            # One instrument failing must never kill the others or the loop - but we
            # do NOT count this toward the data outage alert, or an unrelated code
            # error would fire a misleading "both sources down" message.
            log.exception("[%s] Non-data error this cycle -- continuing, NOT counted "
                          "as a data outage.", name)

        # Respect taapi's free-tier rate limit between instruments.
        if i < len(config.INSTRUMENTS) - 1:
            time.sleep(config.TAAPI_MIN_SECONDS_BETWEEN_REQUESTS)

    if should_save:
        state.save_state(current_state)


def main():
    parser = argparse.ArgumentParser(description="EMA crossover signal service")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously instead of a single cycle")
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between cycles when looping (default: 60)")
    args = parser.parse_args()

    setup_logging()

    if not config.TAAPI_SECRETS:
        log.error("No taapi key found in .env (TAAPI_SECRET) -- cannot start.")
        sys.exit(1)

    log.info("taapi keys loaded: %d (%s).", len(config.TAAPI_SECRETS),
             "primary + backup" if len(config.TAAPI_SECRETS) > 1 else "primary only")

    # Fail at STARTUP. An unknown timeframe would leave us unable to compute a bar's close time, 
    # and the first thing to notice would be a crash while formatting a real signal.
    if config.TIMEFRAME not in TIMEFRAME_SECONDS:
        log.error("TIMEFRAME %r is not one of %s -- cannot start.",
                  config.TIMEFRAME, ", ".join(TIMEFRAME_SECONDS))
        sys.exit(1)

    log.info("Starting: EMA %d/%d on %s bars | instruments: %s | mode: %s",
             config.FAST_MA, config.SLOW_MA, config.TIMEFRAME,
             ", ".join(i["name"] for i in config.INSTRUMENTS),
             "loop" if args.loop else "single cycle")

    if not args.loop:
        run_once()
        return

    # The service polls more often than a bar closes. That is deliberate: a bar
    # closing is not an event we can subscribe to, so we check frequently and rely
    # on Guard 1 to make the redundant checks free.
    bar_seconds = TIMEFRAME_SECONDS[config.TIMEFRAME]
    if bar_seconds % args.interval == 0:
        log.info("Poll interval %ds divides the %s bar (%ds), so one cycle will run "
                 "%ds after every bar close.",
                 args.interval, config.TIMEFRAME, bar_seconds, BAR_CLOSE_OFFSET_SECONDS)
    else:
        log.warning("Poll interval %ds does NOT divide the %s bar (%ds). The service "
                    "still works -- Guard 1 keeps it correct -- but detection latency "
                    "will vary between 0 and %ds instead of staying near %ds.",
                    args.interval, config.TIMEFRAME, bar_seconds,
                    args.interval, BAR_CLOSE_OFFSET_SECONDS)

    while True:
        try:
            run_once(args.interval)
        except Exception:
            log.exception("Unexpected error in cycle -- sleeping and continuing.")
        sleep_until_next_cycle(args.interval)


if __name__ == "__main__":
    main()
