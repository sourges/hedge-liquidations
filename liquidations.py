from typing import Dict, Any, Optional

class LiquidationsDealEngine:
    def __init__(self, symbol: str, main_side: str, base_entry_price: float, base_qty: float, max_dca_steps: int = 4, price_step_distance: float = 1200.0):
        self.symbol = symbol
        self.main_side = main_side.upper()
        self.base_entry_price = base_entry_price
        self.base_qty = base_qty
        self.max_dca_steps = max_dca_steps
        self.price_step_distance = price_step_distance  # Distance in dollars between safety steps
        
        # Track active positions and filled safety steps
        self.dca_fills = []  # List of filled safety orders
        self.posture = "Standard"

    def check_and_reset_deal(self, current_price: float, total_net_pnl: float) -> bool:
        """Checks if the deal has hit the profit target and resets the state for a new cycle."""
        take_profit_threshold = 50.0  # Target profit in USD (can be moved to config later)
        
        if total_net_pnl >= take_profit_threshold:
            print(f"\n🎉 [TAKE PROFIT HIT] Deal target reached with PnL: ${total_net_pnl:,.2f}!")
            print(f"🔄 [DEAL RESET] Clearing {len(self.dca_fills)} safety fills and resetting posture.")
            
            # Reset engine states for the next cycle
            self.base_entry_price = current_price  
            self.dca_fills = []
            self.posture = "Standard"
            return True
            
        return False

    def evaluate_deal_state(self, current_price: float) -> Dict[str, Any]:
        """Evaluates current market price against entry and safety ladder thresholds automatically."""
        
        # 1. Automatically check and trigger safety DCA steps (for a SHORT position, price goes UP)
        if self.main_side == "SHORT":
            adverse_move = current_price - self.base_entry_price
            
            expected_steps = int(adverse_move // self.price_step_distance)
            expected_steps = max(0, min(expected_steps, self.max_dca_steps))
            
            while len(self.dca_fills) < expected_steps:
                step_num = len(self.dca_fills) + 1
                trigger_price = self.base_entry_price + (step_num * self.price_step_distance)
                
                self.dca_fills.append({
                    "step": step_num,
                    "price": trigger_price,
                    "qty": self.base_qty * (1.5 ** step_num)  # Compound scaling per step
                })
                print(f"🚨 [{self.symbol}] [SAFETY TRIGGER] Step {step_num} filled at ${trigger_price:,.2f}!")

            if len(self.dca_fills) > 0:
                self.posture = f"Safety DCA Active (Step {len(self.dca_fills)})"
            else:
                self.posture = "Standard"

        # 2. Calculate total net PnL across base entry + all safety fills
        total_qty = self.base_qty + sum(d['qty'] for d in self.dca_fills)
        total_cost_basis = (self.base_entry_price * self.base_qty) + sum(d['price'] * d['qty'] for d in self.dca_fills)
        current_market_value = total_qty * current_price
        
        if self.main_side == "SHORT":
            total_net_pnl = total_cost_basis - current_market_value
        else:
            total_net_pnl = current_market_value - total_cost_basis

        # 3. Check if we reached take profit and need to reset cycle
        self.check_and_reset_deal(current_price, total_net_pnl)

        # 4. Check PTP (Paired Take-Profit) status
        ptp_status = "REACHABLE" if total_net_pnl >= 0 else "UNREACHABLE AT CURRENT DELTA (Paradox State)"

        return {
            "symbol": self.symbol,
            "current_price": current_price,
            "total_net_pnl_usd": total_net_pnl,
            "ptp_status": ptp_status,
            "posture": self.posture,
            "active_dca_steps": len(self.dca_fills)
        }