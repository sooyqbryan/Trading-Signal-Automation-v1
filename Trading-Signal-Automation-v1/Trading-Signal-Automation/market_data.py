"""Fetching price bars, with a fallback source

Primary taapi.io -> fallback data-api.binance.vision (keyless). BOTH serve Binance's candles
for the SAME pair, which is what makes failover safe: a different exchange quotes slightly
different prices, shifting the EMAs and risking a false crossover. Verified 2026-07-15 that
every CLOSED bar matched to 0.0000. Both are normalised to one candle shape:
    {"timestamp": <int unix seconds>, "close": <float>, "timestampHuman": <str>}
"""
import datetime
import logging
import time

import requests

import config

log = logging.getLogger(__name__)

TAAPI_URL = "https://api.taapi.io/candles"
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

# How many bars to ask for. We only need SLOW_MA + 1 to compute a signal, but we
# fetch far more on purpose -- see the note about EMA seeding below.
CANDLE_COUNT = 100

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = [5, 15]


class PermanentError(RuntimeError):
    """A failure that retrying cannot fix (bad secret, plan doesn't allow this symbol)."""


class AllSourcesFailed(RuntimeError):
    """Every source failed to return candles: all taapi keys AND the fallback.

    A distinct type so the caller can tell a real DATA outage (count it toward the
    outage alert) apart from some OTHER exception in the cycle (a code bug, which
    must NOT masquerade as 'the feed is down').
    """


def _human(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _redact(text):
    """
    Strip EVERY taapi secret out of anything we are about to log or raise.

    taapi takes its secret as a URL PARAMETER, and its error responses echo the
    request URL back to us. Logging that verbatim writes the credential into
    signals.log and journalctl in plaintext. Git-ignoring the log protects the
    repo, not the disk.

    We iterate config.TAAPI_SECRETS rather than redacting one hard-coded key, so
    the backup key (and any future third key) is scrubbed automatically.

    Redact BEFORE truncating, never after: truncating first can slice the secret
    in half, and then there is no full string left to match - so a fragment of
    the credential survives into the log.
    """
    text = str(text)
    for secret in config.TAAPI_SECRETS:
        text = text.replace(secret, "<TAAPI_SECRET_REDACTED>")
    return text


def _with_retry(label, do_request):
    """
    Run `do_request` with retries and backoff.

    We retry the failures worth retrying - network blips, 429 rate limits, 5xx
    server errors. We do NOT retry other 4xx errors: those are our bug or our
    account (bad secret, plan restriction), and retrying just hammers an API with
    a request that can never succeed.
    """
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = do_request()

            if response.status_code == 429:
                last_error = f"429 rate limited: {_redact(response.text)[:150]}"
                log.warning("%s rate limited (attempt %d/%d)", label, attempt, MAX_ATTEMPTS)
            elif 500 <= response.status_code < 600:
                last_error = f"{response.status_code} server error"
                log.warning("%s server error %d (attempt %d/%d)",
                            label, response.status_code, attempt, MAX_ATTEMPTS)
            elif not response.ok:
                raise PermanentError(
                    f"{label} rejected the request ({response.status_code}): "
                    f"{_redact(response.text)[:200]} -- retrying cannot fix this."
                )
            else:
                return response.json()

        except requests.RequestException as e:
            last_error = f"network error: {type(e).__name__}"
            log.warning("%s network error (attempt %d/%d): %s",
                        label, attempt, MAX_ATTEMPTS, type(e).__name__)

        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            log.info("Retrying %s in %ds...", label, wait)
            time.sleep(wait)

    raise RuntimeError(f"{label} failed after {MAX_ATTEMPTS} attempts. Last: {last_error}")


def _fetch_taapi(secret, exchange, symbol, interval):
    """
    Fetch from taapi with a SPECIFIC key and normalise.

    The key is passed in rather than read from config here, because the caller
    tries several keys in turn (primary, then backup). Everything else -- the
    retry/backoff, the redaction, the candle shape -- is identical per key.
    """
    def do():
        return requests.get(TAAPI_URL, params={
            "secret": secret,
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "period": CANDLE_COUNT,
        }, timeout=15)

    data = _with_retry("taapi", do)
    raw = data if isinstance(data, list) else [data]
    return [
        {
            "timestamp": c["timestamp"],
            "close": float(c["close"]),
            "timestampHuman": c.get("timestampHuman") or _human(c["timestamp"]),
        }
        for c in raw
    ]


def _fetch_binance(symbol, interval):
    """
    Fetch from Binance's public data host (fallback) and normalise.

    Note api.binance.com itself is unreachable from here (geo-blocked), but data-api.binance.vision, 
    Binance's public market-data host, serves the same candles and needs no API key.

    A kline is a list: [open_time_ms, open, high, low, close, volume, ...]
    so index 0 is the open time (in MILLISECONDS -- taapi uses seconds, hence the
    // 1000) and index 4 is the close price.
    """
    def do():
        return requests.get(BINANCE_URL, params={
            "symbol": symbol,
            "interval": interval,
            "limit": CANDLE_COUNT,
        }, timeout=10)

    raw = _with_retry("binance.vision", do)
    return [
        {
            "timestamp": k[0] // 1000,
            "close": float(k[4]),
            "timestampHuman": _human(k[0] // 1000),
        }
        for k in raw
    ]


def fetch_closed_candles(instrument, interval):
    """
    Fetch recent candles, return ONLY the closed ones (oldest first), plus which
    source answered and which EXCHANGE that source's prices came from.

    Returns (candles, source_name, exchange_id).

    `exchange_id` matters more than `source_name`. `source_name` is the vendor we
    asked ("taapi"); `exchange_id` is whose order book the prices came from
    ("binance"). taapi and binance.vision are different vendors reading the SAME
    exchange, so their prices are interchangeable and our saved relationship stays
    valid across a failover.

    A source with a DIFFERENT exchange_id would not be interchangeable, and the
    caller uses this value to detect that and re-baseline rather than compare
    across a price discontinuity.

    THE CRITICAL BIT: we drop the final candle. The most recent bar is still
    forming -- its close price is still moving -- so acting on it means acting on
    a number that has not settled. We measured this directly: taapi and Binance
    disagreed by ~$9 on the forming bar while agreeing exactly on every closed one.

    Why fetch 100 bars when EMA(21) only needs 21? An EMA is path-dependent: it
    seeds from a plain average then rolls forward, so the seed slightly colours
    every later value. Each extra bar shrinks the seed's influence by (1 - k). At
    100 bars the EMA(21) seed's remaining influence is about 0.91^79 ~= 0.0006 --
    small enough that our EMA is stable and matches what a charting site draws.
    """
    name = instrument["name"]
    fallback_symbol = instrument.get("fallback_symbol")
    # Both our sources read Binance's order book, hence the same exchange_id.
    primary_exchange = instrument["exchange"]

    candles = source = exchange_id = None

    # --- Try each taapi key in priority order (primary, then backup) --------
    # The backup key exists mainly for when the primary runs out of daily credit,
    # but we escalate to it on ANY primary failure, not just "out of credit".
    # That is deliberate: taapi can signal exhaustion as a 4xx, a 429, OR a 200
    # with an empty/error body, and we cannot reliably tell which. Escalating on
    # every failure covers all of them without guessing taapi's error format.
    for i, secret in enumerate(config.TAAPI_SECRETS):
        which = "primary key" if i == 0 else f"backup key #{i}"
        try:
            fetched = _fetch_taapi(secret, instrument["exchange"],
                                   instrument["symbol"], interval)
            if len(fetched) < 2:
                raise RuntimeError(
                    f"taapi returned {len(fetched)} candles (empty or malformed "
                    f"body): {_redact(repr(fetched))[:150]}"
                )
            candles, source, exchange_id = fetched, "taapi", primary_exchange
            if i > 0:
                log.warning("[%s] taapi %s worked after the primary failed.", name, which)
            break
        except PermanentError as e:
            # Bad/expired key or a plan restriction: retrying cannot fix it, and if
            # EVERY key is like this the service needs a human. Loud, but we still
            # try the next key first.
            log.error("[%s] taapi %s PERMANENTLY rejected -- needs a human: %s",
                      name, which, _redact(str(e))[:200])
        except Exception as e:
            # Network blip, retries exhausted, empty body, malformed JSON: absorb
            # it and try the next source. This is the boundary whose whole job is
            # to swallow the unreliable outside world, so we catch broadly but
            # name the type so a real bug is still debuggable three days later.
            log.warning("[%s] taapi %s failed (%s): %s -- trying next source.",
                        name, which, type(e).__name__, _redact(str(e))[:200])

    # --- All taapi keys failed -> keyless Binance fallback ------------------
    if candles is None:
        if not fallback_symbol:
            raise AllSourcesFailed(
                f"[{name}] all taapi keys failed and no fallback_symbol configured."
            )
        log.warning("[%s] all taapi keys failed -- falling back to Binance's public "
                    "data host (same exchange, same pair, so our EMAs are "
                    "unaffected).", name)
        try:
            fetched = _fetch_binance(fallback_symbol, interval)
        except Exception as e:
            # taapi already exhausted above, and now the fallback is down too:
            # THIS is a genuine total data outage, so name it as one.
            raise AllSourcesFailed(
                f"[{name}] all taapi keys failed AND binance.vision failed: "
                f"{type(e).__name__}: {e}"
            )
        if len(fetched) < 2:
            raise AllSourcesFailed(
                f"[{name}] binance.vision returned {len(fetched)} candles: {fetched!r}"
            )
        candles, source, exchange_id = fetched, "binance.vision", "binance"

    # Both sources return oldest-first, so the LAST item is the still-forming bar.
    closed = candles[:-1]

    log.info("[%s] %d candles from %s (exchange: %s), dropped the forming one "
             "-> newest closed bar %s",
             instrument["name"], len(candles), source, exchange_id,
             closed[-1]["timestampHuman"])
    return closed, source, exchange_id
