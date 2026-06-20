"""
Morning briefing generator.

Composes a <200-word summary of:
  - Open positions with entry prices and unrealized P&L
  - Today's equity vs yesterday
  - Any flagged risk conditions
"""
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")


def compose(open_positions: dict, current_prices: dict[str, float], equity: float) -> str:
    now = datetime.now(TZ).strftime("%a %d %b %Y %H:%M ET")
    lines = [f"*Morning Briefing* — {now}", f"Account equity: *${equity:,.2f}*", ""]

    if not open_positions:
        lines.append("No open positions.")
    else:
        lines.append("*Open positions:*")
        for sym, pos in open_positions.items():
            direction = pos["direction"].upper()
            entry = pos["entry_price"]
            qty = pos["qty"]
            price = current_prices.get(sym, entry)
            if pos["direction"] == "long":
                unrealized = (price - entry) * qty
            else:
                unrealized = (entry - price) * qty
            sign = "+" if unrealized >= 0 else ""
            lines.append(
                f"  {sym} {direction} {qty} @ ${entry:.2f} "
                f"→ ${price:.2f} ({sign}${unrealized:.2f})"
            )

    lines += ["", "_Bot is running. Paper trading only._"]
    return "\n".join(lines)
