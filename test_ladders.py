from liquidations import LiquidationsDealEngine
from config_loader import load_config

def run_ladder_simulation():
    print("🧪 Starting Safety Ladder & Over-Hedge Simulation...\n")
    
    # Load config parameters to stay consistent
    config = load_config()
    
    # Initialize the deal engine with our base parameters
    deal = LiquidationsDealEngine(
        symbol=config['symbol'],
        main_side=config['main_side'],
        base_entry_price=config['base_entry_price'],
        base_qty=config['base_qty']
    )
    
    print(f"📊 Initial Setup: {config['main_side']} on {config['symbol']} @ ${config['base_entry_price']:,.2f}\n")
    
    # Simulate a hostile market movement (e.g., a massive price pump against a short position)
    # Starting at base price and climbing past typical safety trigger levels
    simulated_prices = [
        64000.0,  # Base entry
        64500.0,  # Small move up
        65200.0,  # Should trigger Safety Step 1 (hypothetically)
        66500.0,  # Should trigger Safety Step 2
        68000.0,  # Steeper climb
        70000.0,  # Deep into safety ladder / over-hedge territory
        72500.0,
        69000.0,  # Retrace back down towards profit target
        65000.0,
        63500.0   # Recovery / Take profit zone
    ]
    
    for step, price in enumerate(simulated_prices, start=1):
        print(f"--- Simulation Tick #{step} ---")
        print(f"📈 Simulated Market Price: ${price:,.2f}")
        
        # Evaluate how the engine responds to this price
        state = deal.evaluate_deal_state(current_price=price)
        
        print(f"   Total Net PnL: ${state['total_net_pnl_usd']:,.2f}")
        print(f"   PTP Status   : {state['ptp_status']}")
        print(f"   Active Posture: {state.get('posture', 'Standard')}")
        print("")

if __name__ == "__main__":
    run_ladder_simulation()