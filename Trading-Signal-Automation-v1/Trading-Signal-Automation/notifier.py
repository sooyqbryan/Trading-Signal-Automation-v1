"""Alert delivery to Telegram. Delivery is the other half of reliability — a signal we
computed correctly but failed to deliver is, from the user's point of view, a missed signal."""
import datetime
import logging
import time

import requests

import config

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = [5, 15, 30]


def _utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)


def _human(ts):
    return _utc(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def _human_time(ts):
    return _utc(ts).strftime("%H:%M:%S UTC")


def _duration(seconds):
    """Render a latency the way a human reads it: 211 -> '3m31s'."""
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def send(text):
    """
    Send one message to our Telegram chat.

    Returns True only when Telegram CONFIRMS delivery (ok: true). Returns False
    if every attempt failed.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error("Telegram is not configured (missing token or chat id) -- cannot send.")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, data=payload, timeout=10)

            if response.ok and response.json().get("ok"):
                log.info("Telegram delivery confirmed.")
                return True

            # 429 = Telegram is rate limiting us; it tells us how long to wait.
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limited, it asked us to wait %ss", retry_after)
                time.sleep(retry_after)
                continue

            log.warning(
                "Telegram rejected the message (attempt %d/%d): %s %s",
                attempt, MAX_ATTEMPTS, response.status_code, response.text[:200],
            )

        except requests.RequestException as e:
            log.warning("Network error sending to Telegram (attempt %d/%d): %s",
                        attempt, MAX_ATTEMPTS, e)

        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            log.info("Retrying Telegram in %ds...", wait)
            time.sleep(wait)

    log.error("Telegram delivery FAILED after %d attempts -- state will not be saved, "
              "so the next run will retry this alert.", MAX_ATTEMPTS)
    return False


def format_signal(name, side, bar_time, bar_seconds, price, fast_value, slow_value,
                  previous_rel, fast_len, slow_len, timeframe, source, exchange_id,
                  gap_bars=0):
    """
    Build the alert text.

    Every alert names its data source and exchange. An alert that cannot tell you
    where its numbers came from is not evidence - and if a signal ever fires while
    we are running on the fallback, you should be able to see that at a glance
    rather than cross-referencing a log.

    TIMESTAMPS: a bar is named by the time it OPENS - the exchange convention. So
    a 15m bar called 14:00 does not exist as data until 14:15. We print open AND
    close because printing only the name invites the reader to compare it against
    their watch and conclude the alert was 18 minutes late (it was not: see
    docs/BUGS.md BUG-01, which is exactly how that confusion happened here).

    `Detected` makes detection latency self-documenting: the gap between bar close
    and our seeing the signal is visible in every alert, so nobody has to
    reconstruct it from logs.

    Why "Detected" and not "Sent"? Because this timestamp is taken HERE, while the
    message is being composed -- and composing happens before `send()`, which may
    retry for up to ~50s against its backoff. Labelling it "Sent" would be true
    only when delivery is instant, and would UNDERSTATE the delay in exactly the
    case where delivery was actually slow. That is the BUG-01 defect over again --
    a label asserting something the code does not do -- and it was very nearly
    shipped inside the fix FOR BUG-01. "Detected" is what the clock actually reads.
    """
    emoji = "🟢" if side == "BUY" else "🔴"

    # The GAP is how far apart the two EMAs are at the moment of the cross.
    #
    # Say ONLY what the gap actually supports: that THIS cross was decisive or not.
    # It does NOT tell us the market is "sideways" or "ranging" -- at every crossover
    # the EMAs are near each other by definition, so a small gap means the cross was
    # unconvincing, not that the market is in a range. Claiming otherwise is an
    # inference we cannot back up.
    #
    # We do NOT suppress weak crosses: that would change WHICH signals fire, which is
    # a strategy decision, and every crossover here is genuine.
    gap = abs(fast_value - slow_value)
    gap_pct = gap / price * 100
    conviction = "weak cross, may reverse" if gap_pct < 0.02 else "clear separation"

    bar_close_ts = bar_time + bar_seconds
    detected_ts = time.time()

    # LATE DISCLOSURE: if we were down for more than one bar before this, the
    # flip may have crossed on an EARLIER bar - we alert once on the current bar and
    # never replay. Say so plainly, or a late signal is indistinguishable from a fresh
    # one (the "Detected: Ns after close" line below actively implies freshness).
    # NOTE: we avoid the word "Gap" on purpose - it already means the EMA separation.
    late_note = ""
    if gap_bars and gap_bars > 1:
        late_note = (
            f"⚠️ LATE — the service was down for {gap_bars} bars before this, so this "
            f"flip may have crossed earlier. We alert once on the current bar and never "
            f"replay old crossovers.\n"
            f"\n"
        )

    return (
        f"{emoji} {side} — {name}\n"
        f"\n"
        f"{late_note}"
        f"EMA {fast_len} crossed {'above' if side == 'BUY' else 'below'} EMA {slow_len} "
        f"on the {timeframe} chart.\n"
        f"\n"
        f"Bar open  : {_human(bar_time)}\n"
        f"Bar close : {_human(bar_close_ts)}\n"
        f"Detected  : {_human_time(detected_ts)} "
        f"({_duration(detected_ts - bar_close_ts)} after close)\n"
        f"Price     : {price:,.2f}\n"
        f"EMA {fast_len:<6}: {fast_value:,.2f}\n"
        f"EMA {slow_len:<6}: {slow_value:,.2f}\n"
        f"Gap       : {gap:,.2f} ({gap_pct:.3f}%) — {conviction}\n"
        f"Was       : fast {previous_rel} slow\n"
        f"Source    : {source} ({exchange_id})\n"
        f"\n"
        f"Signal only — not financial advice. Do your own research."
    )


def format_source_change(name, old_source, new_source, exchange_id, is_fallback):
    """
    Tell the user the data source changed, and whether that affects trust.

    This fires on the TRANSITION only (see main.py) -- not every cycle. A source
    outage lasting hours would otherwise send hundreds of identical messages,
    which Telegram would rate-limit, potentially delaying a real signal.
    """
    if is_fallback:
        return (
            f"⚠️ {name} — DATA SOURCE FAILOVER\n"
            f"\n"
            f"{old_source} stopped responding. Switched to {new_source}.\n"
            f"\n"
            f"Exchange  : {exchange_id} (unchanged)\n"
            f"Signals   : still trustworthy — same exchange and pair, so the\n"
            f"            prices and EMAs are identical.\n"
            f"\n"
            f"The service is still running. No action needed right now."
        )
    return (
        f"✅ {name} — DATA SOURCE RECOVERED\n"
        f"\n"
        f"{new_source} is responding again. Switched back from {old_source}.\n"
        f"\n"
        f"Exchange  : {exchange_id} (unchanged)"
    )


def format_data_outage(name, failed_cycles, minutes):
    """
    Tell the user that ALL data sources are down -- once, not every cycle.

    This is the answer to a real gap: when every source fails, the service goes
    quiet, and from the phone a total outage looks exactly like a calm market.
    So we say, explicitly, that we are blind -- but still alive and retrying.

    Sent once per outage (the caller dedupes on a flag), because an outage lasting
    hours must not become hundreds of identical messages that Telegram would then
    rate-limit, delaying the recovery notice or a real signal.
    """
    return (
        f"⚠️ {name} — DATA OUTAGE\n"
        f"\n"
        f"Every data source failed for {failed_cycles} cycles in a row "
        f"(~{minutes} min): both taapi keys AND the Binance fallback.\n"
        f"\n"
        f"The service is ALIVE and retrying every cycle. No signals can be\n"
        f"computed until data returns, so a crossover in this window may be\n"
        f"delayed -- you will get it late, on the current bar, once we recover.\n"
        f"\n"
        f"If this lasts, check the server's network or the data sources.\n"
        f"(A dead SERVER cannot send this message at all -- so receiving it means\n"
        f" the service itself is still up; only the data feed is down.)"
    )


def format_data_recovered(name, failed_cycles, minutes):
    """Tell the user data is flowing again. Paired with format_data_outage."""
    return (
        f"✅ {name} — DATA RECOVERED\n"
        f"\n"
        f"Market data is flowing again after {failed_cycles} failed cycles "
        f"(~{minutes} min blind).\n"
        f"\n"
        f"Normal operation resumed. Any crossover during the outage is handled by\n"
        f"the gap policy: stale bars are skipped, and if the relationship changed\n"
        f"we alert once on the current bar rather than replaying old crossovers."
    )


def format_rebaseline(name, old_exchange, new_exchange, rel, bar_time_human):
    """
    Warn that we crossed an EXCHANGE boundary and deliberately suppressed a signal.

    Different exchanges have different prices, so a saved "fast above slow" from
    one exchange cannot be compared against EMAs from another -- that comparison
    could invent a crossover that never happened. We re-baseline instead and say
    so plainly.
    """
    return (
        f"🔄 {name} — EXCHANGE CHANGED, BASELINE RESET\n"
        f"\n"
        f"Prices now come from {new_exchange}, previously {old_exchange}.\n"
        f"\n"
        f"Different exchanges quote slightly different prices, so comparing the\n"
        f"old state against the new one could fire a false signal. We reset the\n"
        f"baseline instead.\n"
        f"\n"
        f"Bar       : {bar_time_human}\n"
        f"Now       : fast EMA is {rel} slow EMA\n"
        f"Effect    : any crossover on THIS bar was suppressed on purpose.\n"
        f"            Crossover detection resumes on the next bar."
    )
