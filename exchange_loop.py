import time
import ccxt
from liquidations import LiquidationsDealEngine
from config_loader import load_config
from state_manager import StateManager

def run_monitoring_daemon():
    # 1. Load external configuration
    config = load_config()

    # 2. Initialize State Manager for persistence
    state_mgr = StateManager("deal_state.json")
    
    # Optional: Try loading previous state if a restart happened
    previous_state = state_mgr.load_state()
    if previous_state:
        print(f"🔄 Restoring previous session data for symbol: {previous_state.get('symbol')}")

    print("🔌 Initializing Exchange Connector...")
    exchange = ccxt.krakenfutures({
        'enableRateLimit': True,
    })

    symbol = config['symbol']
    print(f"📡 Connecting to Kraken Futures stream for {symbol}...")
    print("🤖 Monitoring loop started. Press Ctrl+C to stop safely.\n")

    # 3. Initialize deal engine using config values
    deal = LiquidationsDealEngine(
        symbol=config['symbol'], 
        main_side=config['main_side'], 
        base_entry_price=config['base_entry_price'], 
        base_qty=config['base_qty']
    )

    tick_count = 0

    try:
        while True:
            tick_count += 1
            try:
                # Fetch live price from Kraken Futures
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # Evaluate the current deal and position math
                deal_state = deal.evaluate_deal_state(current_price=current_price)
                
                # 4. Save current state to disk (Persistence)
                state_mgr.save_state(deal_state)
                
                print(f"[Tick #{tick_count}] Price: ${current_price:,.2f} | PnL: ${deal_state['total_net_pnl_usd']:,.2f} | Status: {deal_state['ptp_status']}")

            except ccxt.NetworkError as ne:
                print(f"⚠️ Network warning (connection glitch): {ne}")
            except ccxt.ExchangeError as ee:
                print(f"⚠️ Exchange error response: {ee}")
            except Exception as e:
                print(f"⚠️ Unexpected error in loop: {e}")

            # Use poll interval from config file
            time.sleep(config.get('poll_interval_seconds', 5))

    except KeyboardInterrupt:
        print("\n🛑 Monitoring daemon stopped gracefully by user. State saved to disk.")

if __name__ == "__main__":
    run_monitoring_daemon()