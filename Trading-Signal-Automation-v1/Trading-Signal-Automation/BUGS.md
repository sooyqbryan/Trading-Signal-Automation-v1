# Bugs Log

A log of defects found and fixed while building and running this service.

---

## BUG-01 — the alert says "Bar close" but prints the bar's OPEN time
**Found:** 2026-07-16 14:18 UTC, during the unattended run.
**Fixed:** 2026-07-17, before the clean run.

**Fix applied.** Every alert now carries its own timing evidence:
```
Bar open  : 2026-07-16 14:00:00 UTC
Bar close : 2026-07-16 14:15:00 UTC
Detected  : 14:18:31 UTC (3m31s after close)
```
`format_signal` now takes `bar_time` + `bar_seconds` rather than a pre-formatted string,
because it has to do arithmetic (close = open + one bar), not just print. The `Detected` line
makes latency self-documenting.

---

## BUG-02 — `config.py` named the instrument `BTC/USD` while fetching `BTC/USDT`
**Found:** 2026-07-15, by noticing our price disparency with TradingView.

Binance lists `BTCUSD` and `BTCUSDT` as **separate markets with separate order books**

The config `name` said `BTC/USD`; the code fetched `BTC/USDT`. Since `name` is what the alert
prints, it sent us to the wrong TradingView chart to "verify" against.

**Fix:** `name` corrected to `BTC/USDT`, with a comment in `config.py` explaining why the precision
matters. **To verify against a chart, use `BINANCE:BTCUSDT` ("Bitcoin / TetherUS"), never BTCUSD.**

---

## BUG-03 — EMA rows misaligned in the alert
**Found:** 2026-07-15, when reading a sample alert.

`f"EMA {fast_len:<5}"` left the EMA rows one character short of the other labels, so the columns
didn't line up. **Fix:** `:<6`. Trivial, but the alert is the deliverable and a ragged one reads as
carelessness.

---

## BUG-04 — `api.binance.com` unreachable from Malaysia
**Found:** 2026-07-15. Geo constraint.

`api.binance.com` connect-timed-out from Malaysia. **Fix:** use `data-api.binance.vision`, which is
Binance's public market-data host, needs no API key, and serves identical data.

---

## BUG-05 — Telegram egress was never verified on the production host (near-miss)
**Found:** 2026-07-16, by an agent advisor review.

The reboot test proved **persistence**, not **delivery**. taapi working from the Oracle VM does not
prove Telegram works from it — different host, different egress rules — and the bot token had just
travelled over `scp`. With the baseline at `below` and no crossover yet, **the send path had never
executed on that machine.**

A blocked `api.telegram.org` or a mangled token would have been **indistinguishable from a quiet
market** for the entire 3-day run, and discovered only at the end. That is criterion #1 failing
silently, in disguise.

**Fix:** ran `test_telegram.py` on the VM before declaring the run live → `Telegram replied ok = True`.
Added to `deploy/README.md` as a mandatory check on any new host.

---

## BUG-06 — the taapi secret can appear in log output
**Found:** 2026-07-15, during error testing.

taapi is called with the secret **in the URL**. When a request fails, the error text echoed back
includes the full URL — **secret and all** — and that goes straight to the log.

**Fix:** `market_data._redact()` scrubs the secret from any text before it is logged or put into an
exception message. Applied at all three sites that echo a response body (429 text, permanent-error
text, and the final `RuntimeError` that carries `last_error`).

---

## BUG-07 — detection latency
**Found:** 2026-07-17, alert was sent 18min late

The loop ended with:

```python
time.sleep(args.interval)     # measured from when the cycle FINISHED
```

That is a fixed-**delay** loop, not a fixed-**rate** one. Each cycle takes `interval + work`, so
the poll slips a couple of seconds every time. Two consequences:

1. **The offset from bar close was set by whenever the service happened to boot.** 300s divides
   900s evenly, so the offset barely moved — it drifted ~6s per bar, taking over a day to wander
   through the full 0–300s range. The 3m31s latency was not an average. It was an unlucky
   starting phase that would have *persisted for hours*. Booting three minutes earlier would have
   produced ~30s and no one would ever have asked the question.
2. **The drift was invisible.** No log line could say "you are polling at a bad phase", because
   every individual cycle was behaving exactly as written.

**Fix:** `sleep_until_next_cycle()` anchors each wake to the **wall clock** instead of to the end
of the previous cycle. The Unix epoch starts at 00:00 UTC and bar lengths divide the day evenly,
so when the interval divides the bar length, one poll lands 20s after **every** close, forever.
Drift cannot accumulate, because each sleep is recomputed from the clock rather than from the last
sleep. Verified over 500 simulated cycles with randomised work durations: the wake offset stayed
pinned at exactly 20s. The 20s offset (not 0s) gives the exchange a moment to publish the finished
bar, and absorbs clock skew.

---

## BUG-8 — a late signal looks exactly like a fresh one

**Found:** 2026-07-17, when hosting locally and went off.

**The alert does not disclose the gap.** After a day outage the user gets a BUY that is
indistinguishable from a cross that just happened, and the `Detected: Ns after close` line
actively implies freshness. The gap *is* in `signals.log`, but the log is not where the user is;
they are on their phone, holding the alert.

**FIXED 2026-07-20.** `format_signal` now prepends a `⚠️ LATE — the service was down for N bars…`
line whenever `gap_bars > 1`, so a late signal announces itself instead of masquerading as fresh.
The plumbing: `process_instrument` computes `gap_bars` and passes it through. The pinning test
`test_KNOWN_GAP_IS_NOT_DISCLOSED_IN_THE_ALERT` was **flipped** to
`test_gap_crossover_IS_disclosed_as_late_in_the_alert` (asserts the disclosure IS present, names the
bar count, and does not collide with the EMA-separation "Gap"). A companion assertion pins that a
normal on-time crossover carries **no** late note. Deployed to the VM in the 2026-07-20 hardening
batch and running under tag `run-start-hardened`.

---

## BUG-9 — an empty taapi body silently bypasses the fallback

**Found:** 2026-07-20, while reviewing the test suite.

An empty taapi response is a **200 OK**, so `_fetch_taapi` returns `[]` and raises **nothing**. The
"not enough candles" `RuntimeError` is then raised by `fetch_closed_candles` **after** the
`try/except` that triggers failover (`market_data.py:227` vs the handlers at 205–225). So taapi
answering `200 []` breaks **every cycle until taapi itself recovers** — while `binance.vision` sits
there healthy and is **never even asked**.

**Fixed** as a byproduct of adding the backup-key feature: the "too few candles" check moved
*inside* the per-key try in `fetch_closed_candles`, so an empty `200 []` body is now just another
failure that escalates — primary key → backup key → binance.vision. The pinning test was flipped
from `test_KNOWN_empty_taapi_body_does_NOT_trigger_the_fallback` to
`test_empty_taapi_body_now_triggers_the_fallback`, which asserts the fallback IS reached. Not yet on
the VM.

---

## BUG-10 — a total data outage is invisible from the phone

**Implemented** in `main.py`: an in-memory per-instrument counter (`_consecutive_failures`,
`_in_outage`) increments on a fully-failed cycle (all taapi keys AND binance dead) and sends ONE
`format_data_outage` alert at `OUTAGE_ALERT_AFTER = 3` cycles (~15 min), then one
`format_data_recovered` on the next healthy cycle. Deduped, informational-only (never vetoes a
save). Recorded end-to-end in `docs/evidence/outage-demo.log` — 12 failure log-lines produce exactly
2 phone alerts. Tests: `test_a_single_failed_cycle_does_not_alert`,
`test_outage_alerts_exactly_once_after_the_threshold`, `test_recovery_notice_sent_once_when_data_returns`.

**Disclosed consequence:** the counter is in-memory by design. A VM restart *during* an outage 
(e.g. the apt-daily bounce that already happened once) resets the counter and clears the in-outage
flag — so the recovery notice for that specific outage can be *skipped*, and a still-ongoing outage 
would re-arm and re-alert after 3 more cycles. Acceptable: it is informational, and persisting it would 
risk a stale "we're down" alert firing after a clean restart. Named here so it is disclosed, not discovered.

---

