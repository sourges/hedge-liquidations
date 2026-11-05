"""
Kraken Futures liquidation logger.

Watches the public trade feed for liquidation-tagged trades and writes one CSV
row per liquidation, with the VWMA distance (Wick Hunter style: hlc3, N closed
candles of a timeframe) and open interest at that moment.

Logging only: it places no orders and needs no API key.

Run:   python liq_logger.py
Stop:  Ctrl+C
"""

import asyncio
import csv
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "logger_config.json"

WS_URL = "wss://futures.kraken.com/ws/v1"
CHARTS_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/{tf}"

TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400,
}

CSV_COLUMNS = [
    "logged_at_utc",      # when we received it
    "trade_time_utc",     # exchange timestamp of the trade
    "symbol",
    "side",               # raw side as Kraken reports it (verify meaning!)
    "qty",                # base units (e.g. SOL) for PF_ contracts
    "price",
    "notional_usd",       # qty * price
    "vwma",
    "dist_from_vwma_pct", # negative = below VWMA
    "mark_price",
    "open_interest",
    "oi_change_60s_pct",
    "uid",                # Kraken trade id, for de-duplicating
]

OI_WINDOW_SECONDS = 60
NO_MESSAGE_TIMEOUT = 90   # reconnect if the feed goes silent this long

log = logging.getLogger("liq_logger")


# ---------------------------------------------------------------- config / setup

def load_config():
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    if cfg["vwma_timeframe"] not in TF_SECONDS:
        raise ValueError(f"vwma_timeframe must be one of {list(TF_SECONDS)}")
    return cfg


def setup_logging(log_dir):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    log.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(log_dir / "logger.log")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def utc_iso(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- VWMA

def fetch_closed_candles(symbol, tf, period):
    """Return the last `period` *closed* candles (oldest first)."""
    tf_sec = TF_SECONDS[tf]
    now = time.time()
    start = now - tf_sec * (period + 3)          # a few extra in case of gaps
    url = CHARTS_URL.format(symbol=symbol, tf=tf)
    r = requests.get(url, params={"from": int(start), "to": int(now)}, timeout=15)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    # candle "time" is the open time in ms; a candle is closed once open + tf has passed
    closed = [c for c in candles if c["time"] / 1000 + tf_sec <= now]
    return closed[-period:]


def compute_vwma(candles):
    """Volume-weighted moving average of hlc3, same as TradingView vwma(hlc3, n)."""
    pv = 0.0
    vol_total = 0.0
    for c in candles:
        high, low, close, vol = (float(c[k]) for k in ("high", "low", "close", "volume"))
        hlc3 = (high + low + close) / 3
        pv += hlc3 * vol
        vol_total += vol
    return pv / vol_total if vol_total > 0 else None


# ---------------------------------------------------------------- per-pair state

class PairState:
    def __init__(self, symbol):
        self.symbol = symbol
        self.vwma = None
        self.vwma_updated = 0.0
        self.mark_price = None
        self.oi_history = deque()   # (ts, open_interest)
        self.liq_count = 0

    def update_oi(self, ts, oi):
        self.oi_history.append((ts, oi))
        # keep a little more than the window so there's always a reference point
        while self.oi_history and self.oi_history[0][0] < ts - OI_WINDOW_SECONDS * 2:
            self.oi_history.popleft()

    @property
    def open_interest(self):
        return self.oi_history[-1][1] if self.oi_history else None

    def oi_change_pct(self, now):
        """% change in OI over roughly the last 60s, or None if not enough history."""
        if not self.oi_history:
            return None
        current = self.oi_history[-1][1]
        reference = None
        for ts, oi in self.oi_history:
            if ts >= now - OI_WINDOW_SECONDS:
                reference = (ts, oi)
                break
        if reference is None or now - reference[0] < OI_WINDOW_SECONDS * 0.75:
            return None   # history too short (e.g. just after a restart)
        if reference[1] == 0:
            return None
        return (current - reference[1]) / reference[1] * 100


# ---------------------------------------------------------------- CSV output

class DailyCsv:
    """Appends rows to logs/liqs_YYYY-MM-DD.csv (UTC date), one file per day."""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.current_date = None
        self.file = None
        self.writer = None

    def _roll(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self.current_date:
            return
        if self.file:
            self.file.close()
        path = self.log_dir / f"liqs_{today}.csv"
        is_new = not path.exists() or path.stat().st_size == 0
        self.file = open(path, "a", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=CSV_COLUMNS)
        if is_new:
            self.writer.writeheader()
        self.current_date = today
        log.info(f"Writing liquidations to {path}")

    def write(self, row):
        self._roll()
        self.writer.writerow(row)
        self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


# ---------------------------------------------------------------- message handling

def handle_message(msg, states, out, print_raw=False):
    if print_raw:
        log.info(f"RAW {msg}")

    if "event" in msg:
        event = msg["event"]
        if event == "error":
            log.error(f"Kraken error: {msg}")
        elif event in ("subscribed", "info"):
            log.info(f"{event}: {msg.get('feed', '')} {msg.get('product_ids', '')}".strip())
        return

    feed = msg.get("feed")
    symbol = msg.get("product_id")
    state = states.get(symbol)

    if feed == "ticker" and state:
        oi = msg.get("openInterest")
        if oi is not None:
            state.update_oi(time.time(), float(oi))
        if msg.get("markPrice") is not None:
            state.mark_price = float(msg["markPrice"])

    elif feed == "trade" and state:
        if msg.get("type") == "liquidation":
            log_liquidation(msg, state, out)

    # "trade_snapshot" (recent trades sent on subscribe) is skipped on purpose:
    # after a restart it would re-log liquidations we may already have.


def log_liquidation(msg, state, out):
    now = time.time()
    price = float(msg["price"])
    qty = float(msg["qty"])
    trade_ts = msg.get("time", now * 1000) / 1000

    dist = None
    if state.vwma:
        dist = (price - state.vwma) / state.vwma * 100
    oi_chg = state.oi_change_pct(now)

    row = {
        "logged_at_utc": utc_iso(now),
        "trade_time_utc": utc_iso(trade_ts),
        "symbol": state.symbol,
        "side": msg.get("side"),
        "qty": qty,
        "price": price,
        "notional_usd": round(qty * price, 2),
        "vwma": round(state.vwma, 6) if state.vwma else "",
        "dist_from_vwma_pct": round(dist, 3) if dist is not None else "",
        "mark_price": state.mark_price if state.mark_price is not None else "",
        "open_interest": state.open_interest if state.open_interest is not None else "",
        "oi_change_60s_pct": round(oi_chg, 4) if oi_chg is not None else "",
        "uid": msg.get("uid", ""),
    }
    out.write(row)
    state.liq_count += 1

    dist_txt = f"{dist:+.2f}% vs VWMA" if dist is not None else "VWMA n/a"
    log.info(f"LIQ {state.symbol} {row['side']} {qty} @ {price} "
             f"(${row['notional_usd']:,.0f}, {dist_txt})")


# ---------------------------------------------------------------- background tasks

async def vwma_refresher(cfg, states):
    tf, period = cfg["vwma_timeframe"], cfg["vwma_period"]
    while True:
        for symbol, state in states.items():
            try:
                candles = await asyncio.to_thread(fetch_closed_candles, symbol, tf, period)
                if len(candles) < period:
                    log.warning(f"{symbol}: only {len(candles)} closed candles, VWMA left unchanged")
                    continue
                vwma = compute_vwma(candles)
                if vwma:
                    state.vwma = vwma
                    state.vwma_updated = time.time()
            except Exception as e:
                log.warning(f"{symbol}: VWMA refresh failed ({e}); keeping previous value")
        await asyncio.sleep(cfg["vwma_refresh_seconds"])


async def status_reporter(cfg, states):
    while True:
        await asyncio.sleep(cfg["status_every_seconds"])
        parts = []
        for s in states.values():
            vwma = f"{s.vwma:.4f}" if s.vwma else "n/a"
            parts.append(f"{s.symbol}: liqs={s.liq_count} vwma={vwma} oi={s.open_interest}")
        log.info("STATUS  " + " | ".join(parts))


# ---------------------------------------------------------------- websocket loop

async def stream(cfg, states, out):
    pairs = cfg["pairs"]
    backoff = 5
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                          max_size=None) as ws:
                log.info("Connected to Kraken Futures websocket")
                for feed in ("trade", "ticker"):
                    await ws.send(json.dumps({"event": "subscribe", "feed": feed,
                                              "product_ids": pairs}))
                await ws.send(json.dumps({"event": "subscribe", "feed": "heartbeat"}))
                backoff = 5

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=NO_MESSAGE_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning(f"No messages for {NO_MESSAGE_TIMEOUT}s, reconnecting")
                        break
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    handle_message(msg, states, out, cfg.get("print_raw_messages", False))

        except Exception as e:
            log.warning(f"Websocket error: {e!r}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main():
    cfg = load_config()
    log_dir = HERE / cfg["log_dir"]
    setup_logging(log_dir)
    log.info(f"Starting liquidation logger for {', '.join(cfg['pairs'])} "
             f"(VWMA {cfg['vwma_period']} x {cfg['vwma_timeframe']}, hlc3)")

    states = {p: PairState(p) for p in cfg["pairs"]}
    out = DailyCsv(log_dir)
    try:
        await asyncio.gather(
            vwma_refresher(cfg, states),
            status_reporter(cfg, states),
            stream(cfg, states, out),
        )
    finally:
        out.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
