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

    def calc_position_size(self, atr: float, price: float, equity: float | None = None, fractional: bool = False) -> int | float:
        """
        Size so that a 1-ATR adverse move = risk_per_trade_pct * equity.

        shares = (equity * risk_pct) / atr
        Then cap so that a hard_stop_pct * equity loss also limits exposure.
        Pass equity to avoid an extra API call when it was already fetched this loop.
        """
        if atr <= 0 or price <= 0:
            logger.warning("Invalid atr=%.6f or price=%.6f, returning 0 shares", atr, price)
            return 0

        if equity is None:
            equity = self.get_equity()
        risk_dollars = equity * RISK["risk_per_trade_pct"]

        atr_based_shares = risk_dollars / atr

        # Hard cap: don't risk more than hard_stop_pct of equity on a single trade
        hard_stop_dollars = equity * RISK["hard_stop_pct"]
        hard_stop_shares = hard_stop_dollars / price

        shares = min(atr_based_shares, hard_stop_shares)
        if fractional:
            qty: int | float = max(0.001, round(shares, 4))
        else:
            qty = max(1, int(shares))
        logger.info(
            "Position size: equity=%.2f risk=$%.2f atr=%.4f → %g shares (price=%.2f)",
            equity, risk_dollars, atr, qty, price,
        )
        return qty

    # ------------------------------------------------------------------
    # Hard stop price
    # ------------------------------------------------------------------

    def calc_hard_stop(self, direction: str, entry_price: float, qty: int, equity: float | None = None) -> float:
        """Return the hard stop price that limits total loss to hard_stop_pct of equity."""
        if equity is None:
            equity = self.get_equity()
        # Dollar loss budget divided by share count gives the per-share stop distance
        stop_distance = (equity * RISK["hard_stop_pct"]) / max(qty, 1)
        if direction == "long":
            return entry_price - stop_distance
        return entry_price + stop_distance

    # ------------------------------------------------------------------
    # Correlation filter
    # ------------------------------------------------------------------

    MAX_CONCURRENT_POSITIONS = 4

    def correlation_filter_allows(self, new_symbol: str, new_direction: str, open_positions: dict, equity: float | None = None) -> bool:
        """
        Block new entries when the portfolio is already at max concurrent positions,
        or when adding a new crypto long while SPY and QQQ are both long.
        equity param is unused here but accepted for call-site uniformity.
        """
        if len(open_positions) >= self.MAX_CONCURRENT_POSITIONS:
            logger.info(
                "Correlation filter: blocking %s — max %d concurrent positions reached",
                new_symbol, self.MAX_CONCURRENT_POSITIONS,
            )
            return False

        crypto_symbols = {"BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"}
        if new_symbol not in crypto_symbols or new_direction != "long":
            return True

        spy_long = open_positions.get("SPY", {}).get("direction") == "long"
        qqq_long = open_positions.get("QQQ", {}).get("direction") == "long"

        if spy_long and qqq_long:
            logger.info(
                "Correlation filter: blocking %s long — SPY and QQQ are both long", new_symbol
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
