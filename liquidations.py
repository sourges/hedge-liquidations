import math
from typing import Dict, Any, Optional

class LiquidationsDealEngine:
    def __init__(self, symbol: str, main_side: str, base_entry_price: float, base_qty: float, target_profit_usd: float = 0.50):
        self.symbol = symbol.upper()
        self.main_side = main_side.upper()  # "SHORT" or "LONG"
        self.main_qty = base_qty
        self.main_avg_entry = base_entry_price
        self.target_profit_usd = target_profit_usd
        
        # DCA Safety Ladder State
        self.dca_count = 0
        self.max_dca = 4
        
        # Counter-Hedge State
        self.is_hedged = False
        self.hedge_side = "LONG" if self.main_side == "SHORT" else "SHORT"
        self.hedge_qty = 0.0
        self.hedge_avg_entry = 0.0
        
        # Realized Buffer (Funding fees, closed partials)
        self.realized_pnl_usd = 0.0

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

    def evaluate_deal_state(self, current_price: float) -> Dict[str, Any]:
        """
        Evaluates current unrealized PnL, calculates net delta, 
        and solves for the target Paired Take-Profit (PTP) price.
        """
        # 1. Calculate Main Leg PnL
        main_dir = -1.0 if self.main_side == "SHORT" else 1.0
        main_pnl = self.main_qty * (current_price - self.main_avg_entry) * main_dir

        # 2. Calculate Hedge Leg PnL
        hedge_pnl = 0.0
        if self.is_hedged and self.hedge_qty > 0:
            hedge_dir = 1.0 if self.hedge_side == "LONG" else -1.0
            hedge_pnl = self.hedge_qty * (current_price - self.hedge_avg_entry) * hedge_dir

        total_net_pnl = main_pnl + hedge_pnl + self.realized_pnl_usd

        # 3. Net Delta Calculation (Signed Quantity)
        main_signed_qty = self.main_qty * main_dir
        hedge_signed_qty = self.hedge_qty * (1.0 if self.hedge_side == "LONG" else -1.0)
        net_delta_qty = main_signed_qty + hedge_signed_qty

        # 4. PTP Solver & Feasibility Check
        if abs(net_delta_qty) < 1e-8:
            ptp_status = "UNREACHABLE (Delta Completely Neutral)"
            calculated_ptp = None
        else:
            constant_term = (self.main_qty * self.main_avg_entry * main_dir) + \
                            (self.hedge_qty * self.hedge_avg_entry * (1.0 if self.hedge_side == "LONG" else -1.0))
            
            calculated_ptp = (self.target_profit_usd - self.realized_pnl_usd + constant_term) / net_delta_qty

            # Directional Solvency Checking (Updated with >= and <= to handle exact target hits)
            if net_delta_qty > 0 and calculated_ptp >= current_price:
                ptp_status = "REACHABLE (Net Long Posture)"
            elif net_delta_qty < 0 and calculated_ptp <= current_price:
                ptp_status = "REACHABLE (Net Short Posture)"
            elif abs(calculated_ptp - current_price) < 1e-4:
                ptp_status = "TARGET REACHED / MET"
            else:
                ptp_status = "UNREACHABLE AT CURRENT DELTA (Paradox State)"

        return {
            "symbol": self.symbol,
            "current_price": current_price,
            "main_unrealized_pnl": round(main_pnl, 2),
            "hedge_unrealized_pnl": round(hedge_pnl, 2),
            "total_net_pnl_usd": round(total_net_pnl, 2),
            "net_delta_qty": round(net_delta_qty, 4),
            "ptp_status": ptp_status,
            "target_ptp_price": round(calculated_ptp, 5) if calculated_ptp else None,
            "notional_hedge_ratio": f"{(self.hedge_qty / self.main_qty) * 100:.1f}%" if self.main_qty > 0 else "0%"
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