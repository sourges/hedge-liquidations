# Liquidation logger

Logs every liquidation trade on Kraken Futures perps to a daily CSV, with the
distance from VWMA (hlc3, same as the Wick Hunter indicator) and open interest.
Logging only: no orders, no API key.

## Run

    pip install -r requirements.txt
    python liq_logger.py

Output goes to `logs/`:
- `liqs_YYYY-MM-DD.csv` - one row per liquidation (UTC dates)
- `logger.log` - connection events, errors, and a status line every 5 minutes

## Settings (`logger_config.json`)

- `pairs` - Kraken perp symbols to watch
- `vwma_timeframe` / `vwma_period` - e.g. 1h x 14 = Wick Hunter default
- `print_raw_messages` - set true to see every raw websocket message (noisy;
  use it once to confirm field names, then turn it off)

## Analysis

    python analyze_liqs.py                 # reads ./logs, writes logs/analysis/
    python analyze_liqs.py --no-coinbase   # skip the Coinbase comparison

Fee assumption and special-event windows (e.g. the Binance hack) are set at the
top of `analyze_liqs.py`.

## Tests

    python -m unittest test_analyze_liqs -v
