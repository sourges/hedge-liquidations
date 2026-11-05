r"""
Liquidation analysis: did fading a Kraken liquidation burst pay off?

Reads the logger's daily CSVs, groups fills into liquidation *bursts*, downloads
Kraken 1-minute candles, and measures what price did 5 / 15 / 60 minutes after
each burst, from the point of view of FADING it:
    longs liquidated (side = sell)  -> fade = go LONG
    shorts liquidated (side = buy)  -> fade = go SHORT

Run:   python analyze_liqs.py            (uses ./logs)
       python analyze_liqs.py path\to\logs

Writes logs/analysis/bursts.csv (one row per burst) and logs/analysis/summary.txt.
Candles are cached in logs/analysis/candles/, so re-runs are fast.
Read-only market data: no API key, no orders.
"""

import csv
import glob
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CHARTS_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/1m"

BURST_GAP_SECONDS = 60       # fills of the same pair+side closer than this = one burst
CLUSTER_GAP_SECONDS = 120    # bursts on any pair within this = one market-wide event
HORIZONS_MIN = [5, 15, 60]
EXCURSION_MIN = 60           # window for best/worst move after the burst

STRETCH_BUCKETS = [(-99, 0, "wrong side of VWMA"), (0, 1, "0-1%"), (1, 2, "1-2%"),
                   (2, 3, "2-3%"), (3, 99, "3%+")]
SIZE_BUCKETS = [(0, 5_000, "< $5k"), (5_000, 25_000, "$5k-25k"),
                (25_000, 100_000, "$25k-100k"), (100_000, 1e18, "$100k+")]


# ------------------------------------------------------------------ load + group

def parse_ts(s):
    return datetime.fromisoformat(s).timestamp()


def load_rows(log_dir):
    rows = []
    for path in sorted(glob.glob(str(log_dir / "liqs_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                r["ts"] = parse_ts(r["trade_time_utc"])
                for k in ("qty", "price", "notional_usd", "vwma", "dist_from_vwma_pct",
                          "open_interest", "oi_change_60s_pct"):
                    r[k] = float(r[k]) if r.get(k) not in ("", None) else None
                rows.append(r)
    # drop duplicates (same trade id) in case files overlap
    seen, unique = set(), []
    for r in rows:
        if r["uid"] and r["uid"] in seen:
            continue
        seen.add(r["uid"])
        unique.append(r)
    return sorted(unique, key=lambda r: r["ts"])


def group_bursts(rows):
    """Consecutive fills on the same symbol+side, each within BURST_GAP_SECONDS
    of the previous one, become one burst."""
    open_bursts, bursts = {}, []
    for r in rows:
        key = (r["symbol"], r["side"])
        b = open_bursts.get(key)
        if b and r["ts"] - b["fills"][-1]["ts"] <= BURST_GAP_SECONDS:
            b["fills"].append(r)
        else:
            b = {"symbol": r["symbol"], "side": r["side"], "fills": [r]}
            open_bursts[key] = b
            bursts.append(b)

    out = []
    for b in bursts:
        f = b["fills"]
        fade = "long" if b["side"] == "sell" else "short"
        dists = [x["dist_from_vwma_pct"] for x in f if x["dist_from_vwma_pct"] is not None]
        ois = [x["oi_change_60s_pct"] for x in f if x["oi_change_60s_pct"] is not None]
        # how far price was stretched *in the direction we'd fade*
        if dists:
            extreme = min(dists) if fade == "long" else max(dists)
            stretch = -extreme if fade == "long" else extreme
        else:
            stretch = None
        out.append({
            "symbol": b["symbol"],
            "side": b["side"],
            "fade": fade,
            "start_ts": f[0]["ts"],
            "end_ts": f[-1]["ts"],
            "fills": len(f),
            "notional_usd": sum(x["notional_usd"] for x in f),
            "first_price": f[0]["price"],
            "last_price": f[-1]["price"],
            "vwma": f[0]["vwma"],
            "stretch_pct": stretch,
            "oi_change_min_pct": min(ois) if ois else None,
        })
    return out


def assign_clusters(bursts):
    """Bursts on any pair that start close together = one market-wide event."""
    cluster, last_end = 0, None
    for b in sorted(bursts, key=lambda b: b["start_ts"]):
        if last_end is None or b["start_ts"] - last_end > CLUSTER_GAP_SECONDS:
            cluster += 1
        b["cluster"] = cluster
        last_end = max(last_end or 0, b["end_ts"])
    return cluster


# ------------------------------------------------------------------ candles

def fetch_candles(symbol, start_ts, end_ts, cache_dir):
    """All 1m candles between start and end (open-time ms -> candle dict).
    Fetched in chunks; each chunk cached on disk."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    candles = {}
    chunk = 12 * 3600                      # 12h per request
    t = int(start_ts // 60 * 60)
    while t < end_ts:
        t_end = min(t + chunk, int(end_ts) + 60)
        cache_file = cache_dir / f"{symbol}_{t}_{t_end}.json"
        complete = t_end < time.time() - 120     # only cache finished chunks
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
        else:
            data = download_range(symbol, t, t_end)
            if complete:
                cache_file.write_text(json.dumps(data))
            time.sleep(0.3)                # be polite to the API
        for c in data:
            candles[int(c["time"])] = c
        t = t_end
    return candles


def download_range(symbol, start, end):
    """Download [start, end) seconds, following up if the API returns a partial page."""
    url = CHARTS_URL.format(symbol=symbol)
    out, cursor = [], start
    for _ in range(50):
        r = requests.get(url, params={"from": cursor, "to": end}, timeout=20)
        r.raise_for_status()
        got = r.json().get("candles", [])
        if not got:
            break
        out.extend(got)
        last = int(got[-1]["time"]) // 1000
        if last + 60 >= end or last < cursor:
            break
        cursor = last + 60
    return out


def candle_at(candles, ts):
    """The candle whose minute contains ts (or None)."""
    key = int(ts // 60 * 60) * 1000
    return candles.get(key)


# ------------------------------------------------------------------ outcomes

def fade_return(fade, entry, price):
    move = (price - entry) / entry * 100
    return move if fade == "long" else -move


def measure(b, candles):
    """Fill in outcome columns for one burst. Entry = last liquidation price."""
    entry = b["last_price"]
    now = time.time()
    for h in HORIZONS_MIN:
        target = b["end_ts"] + h * 60
        c = candle_at(candles, target) if target < now - 60 else None
        b[f"ret_{h}m_pct"] = fade_return(b["fade"], entry, float(c["close"])) if c else None

    # best (MFE) and worst (MAE) move within the window, using highs and lows
    window = [candle_at(candles, b["end_ts"] + m * 60) for m in range(1, EXCURSION_MIN + 1)]
    window = [c for c in window if c]
    if len(window) >= EXCURSION_MIN * 0.8:
        highs = [float(c["high"]) for c in window]
        lows = [float(c["low"]) for c in window]
        if b["fade"] == "long":
            b["best_60m_pct"] = fade_return("long", entry, max(highs))
            b["worst_60m_pct"] = fade_return("long", entry, min(lows))
        else:
            b["best_60m_pct"] = fade_return("short", entry, min(lows))
            b["worst_60m_pct"] = fade_return("short", entry, max(highs))
    else:
        b["best_60m_pct"] = b["worst_60m_pct"] = None


# ------------------------------------------------------------------ summary

def fmt(x, nd=2):
    return f"{x:+.{nd}f}" if x is not None else "  n/a"


def stats_line(label, group):
    def med(key):
        v = [b[key] for b in group if b.get(key) is not None]
        return statistics.median(v) if v else None

    r15 = [b["ret_15m_pct"] for b in group if b.get("ret_15m_pct") is not None]
    win = f"{sum(1 for x in r15 if x > 0) / len(r15) * 100:5.0f}%" if r15 else "  n/a"
    clusters = len({b["cluster"] for b in group})
    return (f"{label:<20}{len(group):>6}{clusters:>9}{win:>9}"
            f"{fmt(med('ret_5m_pct')):>9}{fmt(med('ret_15m_pct')):>9}{fmt(med('ret_60m_pct')):>9}"
            f"{fmt(med('best_60m_pct')):>9}{fmt(med('worst_60m_pct')):>9}")


def bucket(value, buckets):
    if value is None:
        return None
    for lo, hi, name in buckets:
        if lo <= value < hi:
            return name
    return None


def build_summary(bursts, n_clusters, rows):
    header = (f"{'':<20}{'bursts':>6}{'events':>9}{'win15m':>9}"
              f"{'med 5m':>9}{'med 15m':>9}{'med 60m':>9}{'best60':>9}{'worst60':>9}")
    lines = []
    first = datetime.fromtimestamp(rows[0]["ts"], tz=timezone.utc)
    last = datetime.fromtimestamp(rows[-1]["ts"], tz=timezone.utc)
    lines += [
        "LIQUIDATION FADE ANALYSIS",
        f"Data: {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC",
        f"{len(rows)} fills -> {len(bursts)} bursts -> {n_clusters} market-wide events",
        "",
        "All returns are % in the FADE direction, before fees, entering at the last",
        "liquidation price of the burst. win15m = share of bursts up after 15 min.",
        "best60/worst60 = median best and worst point within the next 60 min.",
        "'events' = independent moments (bursts on several pairs at once count once).",
        "",
    ]

    def section(title, key_fn, order):
        lines.append(title)
        lines.append(header)
        for name in order:
            g = [b for b in bursts if key_fn(b) == name]
            if g:
                lines.append(stats_line(name, g))
        lines.append("")

    lines.append(header.replace(" " * 20, "ALL".ljust(20), 1))
    lines.append(stats_line("all bursts", bursts))
    lines.append("")
    section("BY PAIR", lambda b: b["symbol"], sorted({b["symbol"] for b in bursts}))
    section("BY WHO GOT LIQUIDATED", lambda b: "longs (fade long)" if b["fade"] == "long"
            else "shorts (fade short)", ["longs (fade long)", "shorts (fade short)"])
    section("BY STRETCH FROM VWMA (in fade direction)",
            lambda b: bucket(b["stretch_pct"], STRETCH_BUCKETS), [n for _, _, n in STRETCH_BUCKETS])
    section("BY BURST SIZE", lambda b: bucket(b["notional_usd"], SIZE_BUCKETS),
            [n for _, _, n in SIZE_BUCKETS])
    section("BY OPEN INTEREST", lambda b: None if b["oi_change_min_pct"] is None else
            ("OI fell > 0.2%" if b["oi_change_min_pct"] < -0.2 else "OI flat / up"),
            ["OI fell > 0.2%", "OI flat / up"])

    big = sorted(bursts, key=lambda b: -b["notional_usd"])[:15]
    lines.append("15 LARGEST BURSTS")
    lines.append(f"{'time (UTC)':<17}{'pair':<11}{'fade':<6}{'size $':>11}{'stretch':>9}"
                 f"{'5m':>8}{'15m':>8}{'60m':>8}{'worst60':>9}")
    for b in big:
        t = datetime.fromtimestamp(b["start_ts"], tz=timezone.utc)
        lines.append(f"{t:%m-%d %H:%M:%S}   {b['symbol']:<11}{b['fade']:<6}"
                     f"{b['notional_usd']:>11,.0f}{fmt(b['stretch_pct']):>9}"
                     f"{fmt(b['ret_5m_pct']):>8}{fmt(b['ret_15m_pct']):>8}"
                     f"{fmt(b['ret_60m_pct']):>8}{fmt(b['worst_60m_pct']):>9}")
    lines += ["", "Small samples: treat every number here as a first look, not a trading rule."]
    return "\n".join(lines)


# ------------------------------------------------------------------ main

BURST_COLUMNS = ["cluster", "symbol", "side", "fade", "start_utc", "end_utc", "fills",
                 "notional_usd", "first_price", "last_price", "vwma", "stretch_pct",
                 "oi_change_min_pct", "ret_5m_pct", "ret_15m_pct", "ret_60m_pct",
                 "best_60m_pct", "worst_60m_pct"]


def main(log_dir, fetch=fetch_candles):
    out_dir = log_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(log_dir)
    if not rows:
        print(f"No liqs_*.csv rows found in {log_dir}")
        return
    bursts = group_bursts(rows)
    n_clusters = assign_clusters(bursts)
    print(f"{len(rows)} fills -> {len(bursts)} bursts -> {n_clusters} events")

    for symbol in sorted({b["symbol"] for b in bursts}):
        mine = [b for b in bursts if b["symbol"] == symbol]
        start = min(b["start_ts"] for b in mine) - 300
        end = min(max(b["end_ts"] for b in mine) + (EXCURSION_MIN + 2) * 60, time.time())
        print(f"Fetching 1m candles for {symbol} ({len(mine)} bursts)...")
        try:
            candles = fetch(symbol, start, end, out_dir / "candles")
        except Exception as e:
            print(f"  failed: {e} (outcomes for {symbol} left blank)")
            candles = {}
        for b in mine:
            measure(b, candles)

    with open(out_dir / "bursts.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=BURST_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for b in sorted(bursts, key=lambda b: b["start_ts"]):
            row = dict(b)
            row["start_utc"] = datetime.fromtimestamp(b["start_ts"], tz=timezone.utc).isoformat()
            row["end_utc"] = datetime.fromtimestamp(b["end_ts"], tz=timezone.utc).isoformat()
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 4)
            w.writerow(row)

    summary = build_summary(bursts, n_clusters, rows)
    (out_dir / "summary.txt").write_text(summary)
    print()
    print(summary)
    print(f"\nSaved {out_dir / 'bursts.csv'} and {out_dir / 'summary.txt'}")


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "logs"
    main(folder)
