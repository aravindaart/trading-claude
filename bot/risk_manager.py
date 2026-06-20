"""
Risk Manager

ATR-based position sizing: size each position so 1 ATR move = 1% of equity.
Hard stop: every trade capped at 1% account equity loss.
Correlation filter: if SPY and QQQ are both long, block new BTC/USD longs.
"""
import logging

from config import RISK

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, api):
        self._api = api

    # ------------------------------------------------------------------
    # Account equity
    # ------------------------------------------------------------------

    def get_equity(self) -> float:
        account = self._api.get_account()
        return float(account.equity)

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calc_position_size(self, atr: float, price: float) -> int:
        """
        Size so that a 1-ATR adverse move = risk_per_trade_pct * equity.

        shares = (equity * risk_pct) / atr
        Then cap so that a hard_stop_pct * equity loss also limits exposure.
        """
        if atr <= 0 or price <= 0:
            logger.warning("Invalid atr=%.6f or price=%.6f, returning 0 shares", atr, price)
            return 0

        equity = self.get_equity()
        risk_dollars = equity * RISK["risk_per_trade_pct"]

        atr_based_shares = risk_dollars / atr

        # Hard cap: don't risk more than hard_stop_pct of equity on a single trade
        hard_stop_dollars = equity * RISK["hard_stop_pct"]
        hard_stop_shares = hard_stop_dollars / price

        shares = min(atr_based_shares, hard_stop_shares)
        qty = max(1, int(shares))
        logger.info(
            "Position size: equity=%.2f risk=$%.2f atr=%.4f → %d shares (price=%.2f)",
            equity, risk_dollars, atr, qty, price,
        )
        return qty

    # ------------------------------------------------------------------
    # Hard stop price
    # ------------------------------------------------------------------

    def calc_hard_stop(self, direction: str, entry_price: float) -> float:
        """Return the hard stop price that limits loss to 1% of equity."""
        equity = self.get_equity()
        price = entry_price

        # Stop distance in dollars per share such that total loss = hard_stop_pct * equity
        # We'll use price * hard_stop_pct as a simple percentage of entry price as fallback
        stop_distance = price * RISK["hard_stop_pct"]

        if direction == "long":
            return price - stop_distance
        return price + stop_distance

    # ------------------------------------------------------------------
    # Correlation filter
    # ------------------------------------------------------------------

    def correlation_filter_allows(self, new_symbol: str, new_direction: str, open_positions: dict) -> bool:
        """
        Block new BTC/USD long if SPY and QQQ are both already long.
        """
        if new_symbol != "BTC/USD" or new_direction != "long":
            return True

        spy_long = open_positions.get("SPY", {}).get("direction") == "long"
        qqq_long = open_positions.get("QQQ", {}).get("direction") == "long"

        if spy_long and qqq_long:
            logger.info(
                "Correlation filter: blocking BTC/USD long — SPY and QQQ are both long"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Stop-hit check
    # ------------------------------------------------------------------

    def is_hard_stop_hit(self, direction: str, current_price: float, stop_price: float) -> bool:
        if direction == "long":
            return current_price <= stop_price
        return current_price >= stop_price
