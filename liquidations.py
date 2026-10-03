import math
from typing import Dict, Any, Optional

class LiquidationsDealEngine:
    def __init__(self, symbol, main_side, base_entry_price, base_qty, max_dca_steps=4, price_step_distance=1200.0):
        self.symbol = symbol
        self.main_side = main_side.upper()
        self.base_entry_price = base_entry_price
        self.base_qty = base_qty
        self.max_dca_steps = max_dca_steps
        self.price_step_distance = price_step_distance # Distance in dollars between safety steps
        
        # Track active positions and filled safety steps
        self.dca_fills = []  # List of filled safety orders
        self.posture = "Standard"

    def process_dca_fill(self, fill_qty: float, fill_price: float):
        """Simulates filling a safety DCA order and recalculates the main average entry price."""
        if self.dca_count >= self.max_dca:
            print(f"⚠️ [{self.symbol}] Max DCA safety fills reached ({self.dca_count}/{self.max_dca}).")
            return

        total_cost = (self.main_qty * self.main_avg_entry) + (fill_qty * fill_price)
        self.main_qty += fill_qty
        self.main_avg_entry = total_cost / self.main_qty
        self.dca_count += 1
        print(f"✅ [{self.symbol}] Safety DCA #{self.dca_count} Filled: New Avg Entry = {self.main_avg_entry:.5f}, Total Qty = {self.main_qty}")

    def update_hedge(self, hedge_qty: float, hedge_price: float):
        """Opens or scales the counter-hedge position (handles over-hedging)."""
        if not self.is_hedged:
            self.is_hedged = True
            self.hedge_qty = hedge_qty
            self.hedge_avg_entry = hedge_price
        else:
            total_cost = (self.hedge_qty * self.hedge_avg_entry) + (hedge_qty * hedge_price)
            self.hedge_qty += hedge_qty
            self.hedge_avg_entry = total_cost / self.hedge_qty

        notional_ratio = (self.hedge_qty / self.main_qty) * 100.0
        print(f"🛡️ [{self.symbol}] Hedge Updated: Size = {self.hedge_qty} ({notional_ratio:.1f}% Notional) @ Avg {self.hedge_avg_entry:.5f}")

    def evaluate_deal_state(self, current_price):
        """Evaluates current market price against entry and safety ladder thresholds."""
        
        # 1. Check if we need to trigger safety DCA steps (for a SHORT position, price goes UP)
        if self.main_side == "SHORT":
            adverse_move = current_price - self.base_entry_price
            
            # Calculate how many safety steps *should* be triggered based on price distance
            expected_steps = int(adverse_move // self.price_step_distance)
            expected_steps = max(0, min(expected_steps, self.max_dca_steps))
            
            # If the market has climbed enough to trigger a new step that hasn't filled yet:
            while len(self.dca_fills) < expected_steps:
                step_num = len(self.dca_fills) + 1
                trigger_price = self.base_entry_price + (step_num * self.price_step_distance)
                
                # Record the new safety DCA fill
                self.dca_fills.append({
                    "step": step_num,
                    "price": trigger_price,
                    "qty": self.base_qty * (1.5 ** step_num) # Optional: compounding size per step
                })
                print(f"🚨 [SAFETY TRIGGER] Step {step_num} filled at ${trigger_price:,.2f}!")

            # 2. Update posture if safety steps are active
            if len(self.dca_fills) > 0:
                self.posture = f"Safety DCA Active (Step {len(self.dca_fills)})"
            else:
                self.posture = "Standard"

        # 3. Calculate total net PnL across base entry + all safety fills
        total_qty = self.base_qty + sum(d['qty'] for d in self.dca_fills)
        
        # Simplified short PnL calculation for demonstration
        # (Total Entry Value - Current Value)
        total_cost_basis = (self.base_entry_price * self.base_qty) + sum(d['price'] * d['qty'] for d in self.dca_fills)
        current_market_value = total_qty * current_price
        
        if self.main_side == "SHORT":
            total_net_pnl = total_cost_basis - current_market_value
        else:
            total_net_pnl = current_market_value - total_cost_basis

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
        


# ==========================================
# TEST SCRIPT: Simulating the ARBUSDT Squeeze
# ==========================================
if __name__ == "__main__":
    print("--- STEP 1: Initialize Main Short Deal ---")
    deal = LiquidationsDealEngine(symbol="ARBUSDT", main_side="SHORT", base_entry_price=0.12307, base_qty=500.0)

    print("\n--- STEP 2: Fill Safety DCA Ladder (4/4) ---")
    deal.process_dca_fill(300.0, 0.13500)
    deal.process_dca_fill(400.0, 0.15000)
    deal.process_dca_fill(451.7, 0.16500)  # Total Main Qty: 1,651.7

    print("\n--- STEP 3: Price Rips to 0.19531 (Initial Under-Hedge Test) ---")
    deal.update_hedge(hedge_qty=1300.0, hedge_price=0.19531)
    state_1 = deal.evaluate_deal_state(current_price=0.19531)
    print(f"📊 Status: {state_1['ptp_status']} | Net PnL: ${state_1['total_net_pnl_usd']}")

    print("\n--- STEP 4: Bot Over-Hedges to Solve Delta Paradox (>100% Notional) ---")
    deal.update_hedge(hedge_qty=2664.2, hedge_price=0.19531)  # Total Hedge: 3,964.2
    state_2 = deal.evaluate_deal_state(current_price=0.19531)
    print(f"📊 Status: {state_2['ptp_status']} | Target PTP: {state_2['target_ptp_price']} | Ratio: {state_2['notional_hedge_ratio']}")

    print("\n--- STEP 5: Price Continues Pumping to Target PTP -> Atomic Close ---")
    target_price = state_2['target_ptp_price'] # Pulls the dynamically calculated target (0.23273)
    final_state = deal.evaluate_deal_state(current_price=target_price)
    
    print(f"🎯 Final State: {final_state['ptp_status']}")
    print(f"💰 Final Net PnL: +${final_state['total_net_pnl_usd']} USDT -> TARGET MET AT {target_price}! TRIGGER ATOMIC CLOSE!")