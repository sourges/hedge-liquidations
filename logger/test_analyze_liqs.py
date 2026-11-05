"""
Tests for analyze_liqs.py. No internet needed: candles are fake.

Run:   python -m unittest test_analyze_liqs -v
"""

import csv
import tempfile
import unittest
from pathlib import Path

import analyze_liqs as A

T0 = 1_770_000_000  # a fixed past UTC timestamp (Feb 2026), on a minute boundary


def candles_from(prices_hlc, start_ts=T0):
    """Build 1m candles: list of (high, low, close), first one opens at start_ts."""
    out, prev = {}, None
    for i, (h, l, c) in enumerate(prices_hlc):
        t = start_ts + i * 60
        o = prev if prev is not None else c
        out[t * 1000] = {"time": t * 1000, "open": str(o), "high": str(h),
                         "low": str(l), "close": str(c), "volume": "1"}
        prev = c
    return out


def flat_candles(price, minutes, start_ts=T0):
    return candles_from([(price, price, price)] * minutes, start_ts)


def burst(symbol="PF_XBTUSD", fade="long", end_ts=T0 + 30, price=100.0, stretch=1.0,
          notional=10_000):
    return {"symbol": symbol, "side": "sell" if fade == "long" else "buy", "fade": fade,
            "start_ts": end_ts, "end_ts": end_ts, "last_price": price, "first_price": price,
            "stretch_pct": stretch, "notional_usd": notional, "cluster": 1}


def row(symbol, side, ts, price=100.0, notional=1000.0, dist=-1.0, oi=-0.1, uid=None):
    return {"symbol": symbol, "side": side, "ts": ts, "price": price, "notional_usd": notional,
            "dist_from_vwma_pct": dist, "oi_change_60s_pct": oi, "vwma": 101.0,
            "uid": uid or f"{symbol}{side}{ts}"}


class TestGrouping(unittest.TestCase):
    def test_fills_close_together_make_one_burst(self):
        rows = [row("PF_SOLUSD", "sell", T0), row("PF_SOLUSD", "sell", T0 + 30),
                row("PF_SOLUSD", "sell", T0 + 80)]
        bursts = A.group_bursts(rows)
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0]["fills"], 3)
        self.assertEqual(bursts[0]["notional_usd"], 3000)

    def test_gap_or_other_side_starts_new_burst(self):
        rows = [row("PF_SOLUSD", "sell", T0), row("PF_SOLUSD", "sell", T0 + 61),
                row("PF_SOLUSD", "buy", T0 + 62)]
        self.assertEqual(len(A.group_bursts(rows)), 3)

    def test_stretch_sign_follows_fade_direction(self):
        long_b = A.group_bursts([row("PF_SOLUSD", "sell", T0, dist=-2.5)])[0]
        short_b = A.group_bursts([row("PF_SOLUSD", "buy", T0, dist=+1.5)])[0]
        self.assertEqual(long_b["fade"], "long")
        self.assertAlmostEqual(long_b["stretch_pct"], 2.5)
        self.assertEqual(short_b["fade"], "short")
        self.assertAlmostEqual(short_b["stretch_pct"], 1.5)

    def test_clusters_join_pairs_that_flush_together(self):
        bursts = A.group_bursts([row("PF_SOLUSD", "sell", T0), row("PF_XBTUSD", "sell", T0 + 50),
                                 row("PF_ETHUSD", "sell", T0 + 500)])
        n = A.assign_clusters(bursts)
        self.assertEqual(n, 2)


class TestReturns(unittest.TestCase):
    def test_fade_return_direction(self):
        self.assertAlmostEqual(A.fade_return("long", 100, 101), 1.0)
        self.assertAlmostEqual(A.fade_return("short", 100, 101), -1.0)

    def test_measure_known_values(self):
        b = {"fade": "long", "last_price": 100.0, "end_ts": T0 + 20}
        c = {}
        for m in range(70):
            t = T0 + m * 60
            close = 100 + m * 0.1
            low = close - 2 if m == 3 else close - 0.1
            c[t * 1000] = {"close": str(close), "high": str(close + 0.5), "low": str(low)}
        A.measure(b, c)
        self.assertAlmostEqual(b["ret_5m_pct"], 0.5)
        self.assertAlmostEqual(b["ret_15m_pct"], 1.5)
        self.assertAlmostEqual(b["ret_60m_pct"], 6.0)
        self.assertAlmostEqual(b["worst_60m_pct"], -1.7)
        self.assertAlmostEqual(b["best_60m_pct"], 6.5)

    def test_net_subtracts_round_trip_fee(self):
        b = {"ret_5m_pct": 0.5, "ret_15m_pct": None, "ret_60m_pct": -0.2}
        A.add_net(b)
        self.assertAlmostEqual(b["ret_5m_net_pct"], 0.5 - A.ROUND_TRIP_FEE_PCT)
        self.assertIsNone(b["ret_15m_net_pct"])
        self.assertAlmostEqual(b["ret_60m_net_pct"], -0.2 - A.ROUND_TRIP_FEE_PCT)


class TestSimulateExit(unittest.TestCase):
    # entry at 100, start_ts = T0 + 30 -> first candle checked is the one at T0 + 60
    def test_long_hits_target(self):
        c = candles_from([(100, 100, 100), (100.2, 99.9, 100.1), (100.6, 100.0, 100.5)])
        self.assertEqual(A.simulate_exit("long", 100, c, T0 + 30, 0.5, 1.0, max_min=2),
                         (0.5, 2, "target"))

    def test_long_hits_stop(self):
        c = candles_from([(100, 100, 100), (100.1, 98.9, 99.0)])
        self.assertEqual(A.simulate_exit("long", 100, c, T0 + 30, 0.5, 1.0, max_min=1),
                         (-1.0, 1, "stop"))

    def test_same_candle_assumes_stop(self):
        c = candles_from([(100, 100, 100), (101.0, 98.0, 100.0)])
        self.assertEqual(A.simulate_exit("long", 100, c, T0 + 30, 0.5, 1.0, max_min=1)[2], "stop")

    def test_short_hits_target(self):
        c = candles_from([(100, 100, 100), (100.1, 99.4, 99.5)])
        self.assertEqual(A.simulate_exit("short", 100, c, T0 + 30, 0.5, 1.0, max_min=1),
                         (0.5, 1, "target"))

    def test_short_hits_stop(self):
        c = candles_from([(100, 100, 100), (101.2, 99.9, 101.0)])
        self.assertEqual(A.simulate_exit("short", 100, c, T0 + 30, 0.5, 1.0, max_min=1),
                         (-1.0, 1, "stop"))

    def test_time_exit_uses_last_close(self):
        c = candles_from([(100, 100, 100)] + [(100.2, 99.9, 100.2)] * 3)
        gross, minutes, reason = A.simulate_exit("long", 100, c, T0 + 30, 1.0, 1.0, max_min=3)
        self.assertEqual((minutes, reason), (3, "time"))
        self.assertAlmostEqual(gross, 0.2)

    def test_too_many_missing_candles_returns_none(self):
        c = candles_from([(100, 100, 100)])
        self.assertIsNone(A.simulate_exit("long", 100, c, T0 + 30, 1.0, 1.0, max_min=10))


class TestRunStrategy(unittest.TestCase):
    def setUp(self):
        # flat at 100, then price pops at minute 6 so a long target is hit there
        prices = [(100, 100, 100)] * 6 + [(101, 100, 100.8)] + [(100.8, 100.8, 100.8)] * 120
        self.candles = {"PF_XBTUSD": candles_from(prices), "PF_ETHUSD": candles_from(prices)}

    def test_one_position_skips_overlapping_bursts(self):
        b1 = burst("PF_XBTUSD", end_ts=T0 + 30)
        b2 = burst("PF_ETHUSD", end_ts=T0 + 90)        # before b1's exit at minute 6
        b3 = burst("PF_ETHUSD", end_ts=T0 + 30 + 400)  # after b1's exit
        t = A.run_strategy([b1, b2, b3], self.candles, tp_pct=0.5, sl_pct=1.0)
        self.assertEqual([x["burst"] for x in t], [b1, b3])

    def test_without_rule_all_bursts_trade(self):
        bs = [burst("PF_XBTUSD", end_ts=T0 + 30), burst("PF_ETHUSD", end_ts=T0 + 90)]
        t = A.run_strategy(bs, self.candles, 0.5, 1.0, one_position=False)
        self.assertEqual(len(t), 2)

    def test_net_is_gross_minus_fees(self):
        t = A.run_strategy([burst(end_ts=T0 + 30)], self.candles, 0.5, 1.0)
        self.assertEqual(t[0]["reason"], "target")
        self.assertAlmostEqual(t[0]["net"], 0.5 - A.ROUND_TRIP_FEE_PCT)

    def test_filters_by_stretch_and_size(self):
        bs = [burst(end_ts=T0 + 30, stretch=0.5), burst(end_ts=T0 + 30, stretch=2.5, notional=100)]
        # only the 2.5% burst passes the stretch filter...
        self.assertEqual(len(A.run_strategy(bs, self.candles, 0.5, 1.0, min_stretch=2.0)), 1)
        # ...and it is too small for a $500 minimum
        self.assertEqual(A.run_strategy(bs, self.candles, 0.5, 1.0, min_stretch=2.0,
                                        min_size=500), [])


class TestHelpers(unittest.TestCase):
    def test_event_window_label(self):
        w = [("Hack", "2026-10-29T19:00:00+00:00", "2026-10-30T04:00:00+00:00")]
        self.assertEqual(A.event_label(A.parse_ts("2026-10-29T20:00:00+00:00"), w), "Hack")
        self.assertEqual(A.event_label(A.parse_ts("2026-10-30T04:00:00+00:00"), w), "")

    def test_coinbase_candle_format(self):
        c = A.coinbase_to_candles([[T0 + 60, 99, 101, 100, 100.5, 7], [T0, 98, 100, 99, 100, 3]])
        self.assertEqual(c[(T0 + 60) * 1000]["high"], 101)
        self.assertEqual(c[(T0 + 60) * 1000]["low"], 99)
        self.assertEqual(c[T0 * 1000]["open"], 99)
        self.assertEqual(c[T0 * 1000]["close"], 100)

    def test_needed_windows_covers_burst(self):
        chunk = 300 * 60
        starts = A.needed_windows([burst(end_ts=T0 + 30)], chunk)
        self.assertTrue(any(s <= T0 + 30 < s + chunk for s in starts))
        self.assertTrue(any(s <= T0 + 62 * 60 < s + chunk for s in starts))

    def test_next_minute_open(self):
        c = candles_from([(100, 100, 100), (101, 99, 100.5)])
        self.assertEqual(A.next_minute_open(c, T0 + 30), 100.0)  # opens at previous close


class TestEndToEnd(unittest.TestCase):
    def test_main_writes_summary_with_all_sections(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cols = ["logged_at_utc", "trade_time_utc", "symbol", "side", "qty", "price",
                    "notional_usd", "vwma", "dist_from_vwma_pct", "mark_price", "open_interest",
                    "oi_change_60s_pct", "uid"]
            with open(d / "liqs_2026-10-29.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerow(["", "2026-10-29T20:00:30+00:00", "PF_XBTUSD", "sell", "1", "100",
                            "50000", "103", "-2.9", "", "1000", "-0.5", "a"])
                w.writerow(["", "2026-10-28T10:00:30+00:00", "PF_XBTUSD", "buy", "1", "100",
                            "2000", "99", "1.0", "", "1000", "0.1", "b"])
                w.writerow(["", "2026-10-28T10:0"])  # half-written line must be skipped

            def fake_kraken(symbol, start, end, cache):
                return flat_candles(100.0, int((end - start) // 60) + 90, int(start // 60 * 60))

            def fake_coinbase(product, bursts, cache):
                s = int(min(b["start_ts"] for b in bursts) // 60 * 60) - 300
                return flat_candles(100.0, 2 * 1440, s)

            summary = A.main(d, fetch=fake_kraken, fetch_cb=fake_coinbase)
            for heading in ("BY PAIR", "SPECIAL EVENTS", "Binance hack", "VWMA BAND TABLE PER PAIR",
                            "TAKE-PROFIT / STOP-LOSS", "TOP 5 TP/SL", "KRAKEN vs COINBASE",
                            "15 LARGEST BURSTS"):
                self.assertIn(heading, summary)
            self.assertTrue((d / "analysis" / "bursts.csv").exists())
            with open(d / "analysis" / "bursts.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["event"] for r in rows}, {"Binance hack", ""})


if __name__ == "__main__":
    unittest.main()
