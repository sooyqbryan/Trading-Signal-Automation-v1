"""
Reliability tests

    python -m unittest test_reliability -v

No API keys, no network, no Telegram. These run offline in about a second.

WHY THESE ARE FAKE-DATA TESTS, NOT LIVE ONES
--------------------------------------------
We cannot ask taapi to time out on command, return an empty body, or produce a
crossover the moment a test needs one. The three graded properties are all about
what happens when the outside world misbehaves, so the misbehaviour has to be
INJECTED. A live test cannot reach them at all.

WHERE WE FAKE
-------------

  1. `requests.get`  -- canned candles, or a raised Timeout, or an empty body.
  2. `notifier.send` -- a spy that records calls and returns a result we choose.

Everything BETWEEN those two seams runs for real: JSON parsing, the forming-bar
drop, our own EMA maths, all five guards, the taapi -> binance failover, and the
atomic state write. That line matters. A test that stubbed out Guard 1 and then
asserted "no duplicate" would only prove the stub works.

`state.STATE_FILE` is redirected to a temp file per test, so these never touch
the real state.json.
"""
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import config
import main
import market_data
import state
from indicators import ema, relationship

BAR = 900  # 15m in seconds
# An arbitrary fixed bar-open time, so tests are deterministic rather than
# depending on when they happen to run. 2026-07-01 00:00:00 UTC.
T0 = 1782950400


# --------------------------------------------------------------------------
# Price fixtures
#
# We do not hand-pick EMA numbers. We build a price path, then ask our REAL
# ema()/relationship() what side it lands on, and assert that the fixture is
# what we claim. If the maths ever changes, the fixture check fails loudly
# instead of the test quietly asserting something meaningless.
# --------------------------------------------------------------------------
def falling(n=100, start=100.0, step=0.5):
    """A steadily falling market -> fast EMA ends BELOW slow."""
    return [start - i * step for i in range(n)]


def rising(n=100, start=100.0, step=0.5):
    """A steadily rising market -> fast EMA ends ABOVE slow."""
    return [start + i * step for i in range(n)]


def falling_then_spike(n=100, spike_len=8):
    """
    A falling market that turns sharply upward at the end.

    This is what a real BUY crossover looks like: the fast EMA reacts to the new
    upward prices before the slow one does, and crosses over it.
    """
    base = falling(n - spike_len)
    last = base[-1]
    return base + [last + (i + 1) * 12.0 for i in range(spike_len)]


def rel_of(closes):
    """The relationship our real code would compute for this price path."""
    fast = ema(closes, config.FAST_MA)[-1]
    slow = ema(closes, config.SLOW_MA)[-1]
    return relationship(fast, slow)


def taapi_candles(closes, first_open=T0):
    """
    Shape a price path the way taapi returns it.

    NOTE: the caller must include one EXTRA trailing candle, because
    fetch_closed_candles drops the last one as still-forming. So the newest
    CLOSED bar is closes[-2].
    """
    out = []
    for i, close in enumerate(closes):
        ts = first_open + i * BAR
        out.append({
            "timestamp": ts,
            "close": close,
            "timestampHuman": datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC"),
        })
    return out


def binance_klines(closes, first_open=T0):
    """The same path in Binance's kline shape: [open_ms, o, h, l, close, ...]."""
    return [
        [(first_open + i * BAR) * 1000, "0", "0", "0", str(close), "0"]
        for i, close in enumerate(closes)
    ]


class FakeResponse:
    """Just enough of a requests.Response for market_data to work with."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        # Redirect state to a temp file so we never touch the real state.json.
        fd, self.state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state_path)  # start each test with NO state file at all
        patcher = mock.patch.object(state, "STATE_FILE", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(
            lambda: os.path.exists(self.state_path) and os.unlink(self.state_path)
        )

        # Retry backoff would make these tests take a minute. The waiting is not
        # what we are testing; the behaviour after the retries is.
        for module in (market_data, main):
            p = mock.patch.object(module.time, "sleep", lambda *_: None)
            p.start()
            self.addCleanup(p.stop)

        # A key must be present or main() bails early; values are irrelevant. We
        # patch BOTH the single name and the LIST, because the fetch layer and the
        # log redactor iterate config.TAAPI_SECRETS (computed at import time, so
        # patching TAAPI_SECRET alone would not update it). Default: ONE key.
        for name, value in (("TAAPI_SECRET", "fake-secret-for-tests"),
                            ("TAAPI_SECRETS", ["fake-secret-for-tests"])):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

        # The outage counters are module-level and would otherwise leak
        # between tests. Reset to a clean slate, and restore on teardown.
        main._consecutive_failures.clear()
        main._in_outage.clear()
        self.addCleanup(main._consecutive_failures.clear)
        self.addCleanup(main._in_outage.clear)

    # -- helpers -----------------------------------------------------------
    def route(self, taapi=None, binance=None):
        """
        Fake requests.get, dispatching on URL.

        `taapi`/`binance` may be a FakeResponse, or an Exception to raise. When a
        list is given for taapi, successive taapi calls consume it in order --
        used to make key 1 fail and key 2 succeed within one cycle.
        """
        taapi_queue = list(taapi) if isinstance(taapi, list) else None

        def fake_get(url, **kwargs):
            if "taapi" in url:
                target = taapi_queue.pop(0) if taapi_queue is not None else taapi
            else:
                target = binance
            if target is None:
                raise AssertionError(f"test did not expect a call to {url}")
            if isinstance(target, Exception):
                raise target
            return target
        return mock.patch.object(market_data.requests, "get", side_effect=fake_get)

    def seed_state(self, bar_time, rel, exchange="binance", source="taapi"):
        s = {}
        state.set_instrument_state(s, "BTC/USDT", bar_time, rel, exchange, source)
        state.save_state(s)

    def saved(self):
        if not os.path.exists(self.state_path):
            return None
        with open(self.state_path) as f:
            return json.load(f).get("BTC/USDT")

    # ==================================================================
    # Fixture sanity — prove the price paths mean what we say they mean
    # ==================================================================
    def test_fixtures_produce_the_relationships_we_claim(self):
        self.assertEqual(rel_of(falling()), "below")
        self.assertEqual(rel_of(rising()), "above")
        self.assertEqual(rel_of(falling_then_spike()), "above",
                         "the spike fixture must actually cross the fast EMA above")

    # ==================================================================
    # BULLET 1 — No duplicate alerts
    # ==================================================================
    def test_crossover_alerts_once_then_never_again_for_the_same_bar(self):
        """A crossover fires once. Re-running the same cycle must not re-fire it."""
        closes = falling_then_spike()
        # Previous bar was 'below'; the newest closed bar is 'above' -> crossover.
        self.seed_state(T0 + 97 * BAR, "below")

        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 1, "the crossover should alert once")
            self.assertIn("BUY", send.call_args[0][0])

            # Same data, same state file: a restart, an overlapping run, a manual
            # re-run. Guard 1 must make it a no-op.
            main.run_once()
            main.run_once()
            self.assertEqual(send.call_count, 1,
                             "re-running the same bar must NOT alert again")

    def test_restart_reloads_state_from_disk_and_does_not_re_alert(self):
        """
        The real restart case: a brand-new process reads state.json off disk.

        setUp/run_once do not share memory here -- load_state() re-reads the file,
        which is exactly what happens when systemd restarts the service.
        """
        closes = falling_then_spike()
        self.seed_state(T0 + 97 * BAR, "below")

        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 1)

        newest_closed = T0 + 98 * BAR
        self.assertEqual(self.saved()["last_processed_bar_time"], newest_closed)
        self.assertEqual(self.saved()["last_relationship"], "above")

        # Simulate the process dying and coming back: fresh load, same candles.
        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 0,
                             "after a restart the same bar must not alert again")

    def test_failed_delivery_does_not_save_state_and_the_next_run_retries(self):
        """
        The other half of 'no duplicates': not losing the signal either.

        If Telegram fails we must NOT record the bar as done -- otherwise the
        alert vanishes silently. State saves only after delivery is confirmed.
        """
        closes = falling_then_spike()
        self.seed_state(T0 + 97 * BAR, "below")
        before = self.saved()

        # Delivery fails.
        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=False) as send:
            main.run_once()
            self.assertEqual(send.call_count, 1)

        self.assertEqual(self.saved(), before,
                         "a failed send must leave state untouched")

        # Next cycle: delivery succeeds. The signal must still go out, exactly once.
        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 1, "the retried alert must be sent")
            self.assertIn("BUY", send.call_args[0][0])

        self.assertEqual(self.saved()["last_processed_bar_time"], T0 + 98 * BAR)

        # And now it is done -- it must not fire a third time.
        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 0)

    # ==================================================================
    # BULLET 2 — No silently missed signals
    # ==================================================================
    def test_first_ever_run_establishes_a_baseline_without_alerting(self):
        """No state = no previous side = nothing has crossed. Alerting would invent a signal."""
        with self.route(taapi=FakeResponse(taapi_candles(falling()))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 0, "the first run must not alert")
        self.assertEqual(self.saved()["last_relationship"], "below")

    def test_flip_on_adjacent_bars_alerts(self):
        """The control case for the gap test below: a normal, uninterrupted crossover."""
        closes = falling_then_spike()
        self.seed_state(T0 + 97 * BAR, "below")  # our last bar is the one just before

        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 1,
                             "an adjacent-bar crossover MUST alert")
            # and a normal, on-time crossover must NOT carry the late disclosure
            self.assertNotIn("late", send.call_args[0][0].lower())

    def test_flip_discovered_across_a_gap_alerts_late_exactly_once(self):
        """
        THE ONE A LIVE RUN CANNOT REACH — it needs downtime spanning a flip.

        We were down for 4 bars. On return the relationship has flipped, but we
        cannot know WHICH bar it flipped on.

        POLICY, as the code actually behaves: skip the stale bars, never replay,
        and alert ONCE on the current relationship. That is "alerting late", which
        the task explicitly permits. It is deliberately the safer of the two
        options: suppressing a still-valid flip would be a silently missed signal,
        which is the #1 graded failure. The cross is real - only its exact bar is
        unknown.
        """
        closes = falling_then_spike()
        self.seed_state(T0 + 94 * BAR, "below")  # 4 bars behind the newest closed bar

        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send, \
                self.assertLogs("signal", level="WARNING") as logs:
            main.run_once()

        self.assertTrue(any("GAP DETECTED" in line for line in logs.output),
                        "a gap must be logged, loudly, not passed over in silence")
        self.assertEqual(send.call_count, 1,
                         "alert exactly once -- never replay a backlog of old crossovers")

        # State must advance to the current bar, or we would re-detect this gap forever.
        self.assertEqual(self.saved()["last_processed_bar_time"], T0 + 98 * BAR)
        self.assertEqual(self.saved()["last_relationship"], "above")

    def test_a_gap_of_any_size_still_alerts_and_never_replays(self):
        """
        The policy must not quietly depend on how long we were down.

        A 200-bar gap is ~2 days. We still alert exactly once, never 200 times.
        """
        for gap_bars in (1, 2, 4, 20, 200):
            with self.subTest(gap_bars=gap_bars):
                self.setUp()  # fresh temp state per case
                closes = falling_then_spike()
                self.seed_state(T0 + (98 - gap_bars) * BAR, "below")
                with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                        mock.patch.object(main.notifier, "send", return_value=True) as send:
                    main.run_once()
                self.assertEqual(send.call_count, 1, f"gap of {gap_bars} bars")

    def test_gap_crossover_IS_disclosed_as_late_in_the_alert(self):
        """
        A crossover discovered ACROSS a downtime gap now discloses that it may be late, 
        so on the phone it is no longer indistinguishable from a fresh one. 

        "Gap" already means the EMA separation in this alert ("Gap: 18.34").
        The late disclosure must NOT reuse that word, or one alert carries two meanings.
        """
        closes = falling_then_spike()
        self.seed_state(T0 + 94 * BAR, "below")   # 4 bars behind -> a real gap

        with self.route(taapi=FakeResponse(taapi_candles(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()

        message = send.call_args[0][0].lower()
        # it now says, in plain words, that this signal may be late...
        self.assertIn("late", message)
        self.assertIn("may have crossed earlier", message)
        self.assertIn("4 bars", message)          # names how long we were down
        # ...without colliding with the EMA-separation wording:
        self.assertNotIn("gap of", message)

    def test_gap_without_a_flip_does_not_alert(self):
        """Down for a while, but the market never changed side: nothing to say."""
        self.seed_state(T0 + 94 * BAR, "below")

        with self.route(taapi=FakeResponse(taapi_candles(falling()))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
            self.assertEqual(send.call_count, 0)
        self.assertEqual(self.saved()["last_processed_bar_time"], T0 + 98 * BAR)

    # ==================================================================
    # BULLET 3 — Data-source failures: no crash, no false signal
    # ==================================================================
    def test_taapi_timeout_falls_back_to_binance_and_still_works(self):
        import requests as _requests
        closes = falling()
        self.seed_state(T0 + 97 * BAR, "below")

        with self.route(taapi=_requests.exceptions.Timeout("taapi timed out"),
                        binance=FakeResponse(binance_klines(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()

        # It kept working, off the fallback...
        self.assertEqual(self.saved()["last_source"], "binance.vision")
        self.assertEqual(self.saved()["last_exchange"], "binance")
        # ...and told us the source changed, without inventing a signal.
        self.assertEqual(send.call_count, 1)
        self.assertIn("FAILOVER", send.call_args[0][0])

    def test_both_sources_down_does_not_crash_and_sends_nothing(self):
        import requests as _requests
        self.seed_state(T0 + 97 * BAR, "below")
        before = self.saved()

        with self.route(taapi=_requests.exceptions.Timeout("down"),
                        binance=_requests.exceptions.Timeout("also down")), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()  # must NOT raise

        self.assertEqual(send.call_count, 0, "a dead data source must not produce an alert")
        self.assertEqual(self.saved(), before, "state must be untouched")

    def test_empty_response_does_not_crash_and_sends_nothing(self):
        self.seed_state(T0 + 97 * BAR, "below")
        before = self.saved()

        with self.route(taapi=FakeResponse([]), binance=FakeResponse([])), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()  # must NOT raise

        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.saved(), before)

    def test_empty_taapi_body_now_triggers_the_fallback(self):
        self.seed_state(T0 + 97 * BAR, "below")
        binance_called = []

        def fake_get(url, **kwargs):
            if "taapi" in url:
                return FakeResponse([])          # 200 OK, empty body
            binance_called.append(url)           # the fallback SHOULD now be asked
            return FakeResponse(binance_klines(falling()))

        with mock.patch.object(market_data.requests, "get", side_effect=fake_get), \
                mock.patch.object(main.notifier, "send", return_value=True):
            main.run_once()

        self.assertTrue(binance_called,
                        "an empty taapi body must now fall through to binance")
        self.assertEqual(self.saved()["last_source"], "binance.vision")
        self.assertEqual(self.saved()["last_exchange"], "binance")

    # ==================================================================
    # BACKUP taapi KEY — used only when the primary fails
    # ==================================================================
    def test_backup_key_is_used_when_the_primary_fails_and_binance_is_not_touched(self):
        """
        Two keys configured. The primary returns an empty body (out of credit
        looks like this, among other shapes); the backup answers. We must stay on
        taapi -- the whole point of a second key -- and never reach binance.
        """
        p = mock.patch.object(config, "TAAPI_SECRETS", ["key-1-dead", "key-2-good"])
        p.start(); self.addCleanup(p.stop)
        self.seed_state(T0 + 97 * BAR, "below")

        # First taapi call (key 1) -> empty; second (key 2) -> real candles.
        with self.route(taapi=[FakeResponse([]),
                               FakeResponse(taapi_candles(falling()))],
                        binance=None), \
                mock.patch.object(main.notifier, "send", return_value=True):
            main.run_once()   # binance=None => AssertionError if binance is touched

        self.assertEqual(self.saved()["last_source"], "taapi",
                         "the backup key keeps us on taapi, not binance")

    def test_backup_key_is_also_redacted_from_logs(self):
        """
        A leaked backup key is the same incident as a leaked primary.
        Redaction must cover EVERY configured key, not just the first.
        """
        backup = "super-secret-backup-key-value"
        p = mock.patch.object(config, "TAAPI_SECRETS", ["primary-key", backup])
        p.start(); self.addCleanup(p.stop)
        self.seed_state(T0 + 97 * BAR, "below")

        # Key 1: a 401 whose body echoes key 1. Key 2: a 401 whose body echoes the
        # BACKUP key. Both must be scrubbed. Then binance saves the cycle.
        leak1 = FakeResponse(None, status_code=401, text="bad: secret=primary-key")
        leak2 = FakeResponse(None, status_code=401, text=f"bad: secret={backup}")
        with self.route(taapi=[leak1, leak2],
                        binance=FakeResponse(binance_klines(falling()))), \
                mock.patch.object(main.notifier, "send", return_value=True), \
                self.assertLogs(level="ERROR") as logs:
            main.run_once()

        blob = "\n".join(logs.output)
        self.assertNotIn(backup, blob, "the BACKUP key must never reach the log")
        self.assertIn("<TAAPI_SECRET_REDACTED>", blob)

    # ==================================================================
    # A total data outage must not be silent from the phone
    # ==================================================================
    def _all_sources_down(self):
        import requests as _requests
        return self.route(taapi=_requests.exceptions.Timeout("down"),
                          binance=_requests.exceptions.Timeout("down"))

    def test_a_single_failed_cycle_does_not_alert(self):
        """One blip is a non-event - the next poll is minutes away. No alert."""
        self.seed_state(T0 + 97 * BAR, "below")
        with self._all_sources_down(), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()
        self.assertEqual(send.call_count, 0)

    def test_outage_alerts_exactly_once_after_the_threshold(self):
        """
        Three back-to-back total failures (~15 min) trigger ONE outage alert.
        A fourth failure must stay silent -- we already told them.
        """
        self.seed_state(T0 + 97 * BAR, "below")
        with self._all_sources_down(), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()   # fail 1
            main.run_once()   # fail 2
            self.assertEqual(send.call_count, 0, "not yet -- below the threshold")
            main.run_once()   # fail 3 -> alert
            self.assertEqual(send.call_count, 1, "exactly one outage alert at the threshold")
            self.assertIn("OUTAGE", send.call_args[0][0])
            main.run_once()   # fail 4
            self.assertEqual(send.call_count, 1, "must NOT re-alert every cycle of the outage")

    def test_recovery_notice_sent_once_when_data_returns(self):
        """After an outage, the first healthy cycle sends a recovery notice, once."""
        self.seed_state(T0 + 97 * BAR, "below")
        with self._all_sources_down(), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            for _ in range(3):
                main.run_once()           # into outage (1 alert)
            self.assertEqual(send.call_count, 1)

        # Data comes back.
        with self.route(taapi=FakeResponse(taapi_candles(falling()))), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()               # recovery notice
            self.assertEqual(send.call_count, 1)
            self.assertIn("RECOVERED", send.call_args[0][0])
            main.run_once()               # normal cycle, no repeat
            self.assertEqual(send.call_count, 1, "recovery is announced once, not every cycle")

    def test_server_error_then_recovery_within_the_same_cycle(self):
        """A 500 is retried, not fatal - the retry loop is doing its job."""
        closes = falling()
        self.seed_state(T0 + 97 * BAR, "below")
        responses = [FakeResponse(None, status_code=500, text="boom"),
                     FakeResponse(taapi_candles(closes))]

        def fake_get(url, **kwargs):
            return responses.pop(0)

        with mock.patch.object(market_data.requests, "get", side_effect=fake_get), \
                mock.patch.object(main.notifier, "send", return_value=True) as send:
            main.run_once()

        self.assertEqual(responses, [], "both responses should have been consumed")
        self.assertEqual(send.call_count, 0, "a 500 must not manufacture a signal")
        self.assertEqual(self.saved()["last_processed_bar_time"], T0 + 98 * BAR)

    def test_permanent_error_falls_back_and_never_leaks_the_secret(self):
        """
        A 401 is permanent - retrying cannot fix a bad key - so we fail over.

        taapi passes the secret as a URL parameter and echoes the URL back in its
        errors, so the error text is a credential-leak path. It must be redacted.
        """
        closes = falling()
        leaky = f"invalid secret: https://api.taapi.io/candles?secret={config.TAAPI_SECRET}"
        self.seed_state(T0 + 97 * BAR, "below")

        with self.route(taapi=FakeResponse(None, status_code=401, text=leaky),
                        binance=FakeResponse(binance_klines(closes))), \
                mock.patch.object(main.notifier, "send", return_value=True), \
                self.assertLogs(level="ERROR") as logs:
            main.run_once()

        blob = "\n".join(logs.output)
        self.assertNotIn(config.TAAPI_SECRET, blob, "the secret must never reach the log")
        self.assertIn("<TAAPI_SECRET_REDACTED>", blob)
        self.assertEqual(self.saved()["last_source"], "binance.vision")


if __name__ == "__main__":
    unittest.main(verbosity=2)
