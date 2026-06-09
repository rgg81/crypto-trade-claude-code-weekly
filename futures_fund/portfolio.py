from __future__ import annotations

from futures_fund.models import PortfolioHealth
from futures_fund.portfolio_risk import position_risk
from futures_fund.state import Position


def unrealized_pnl(position: Position, mark: float) -> float:
    if position.direction == "long":
        return position.qty * (mark - position.entry)
    return position.qty * (position.entry - mark)


def total_equity(balance: float, positions: list[Position], prices: dict[str, float]) -> float:
    """Wallet balance + unrealized PnL of open positions (skips positions with no price)."""
    upnl = 0.0
    for p in positions:
        mark = prices.get(p.symbol)
        if mark is not None:
            upnl += unrealized_pnl(p, mark)
    return balance + upnl


def open_heat(positions: list[Position], equity: float) -> float:
    """Sum of per-position stop-out risk as a fraction of equity (reuses A1 position_risk)."""
    return sum(position_risk(p.qty, p.entry, p.stop, equity, p.direction) for p in positions)


# ALL-WEATHER exposure signal (Pillar 2). The desk's mandate is PROFIT IN ALL CONDITIONS, NOT
# dollar-neutral-always: net exposure is a MANAGED RISK PARAMETER, not a forced zero. This is a SOFT
# diversification nudge — when the book gets materially one-sided in risk-bearing legs, it suggests
# adding a quality OTHER-side setup IF one exists, to reduce concentration. It is NEVER a reason to
# stand flat: a single regime-aligned position with no available hedge is valid and expected.
# Directional desk: this is TELEMETRY, not a neutrality nag. It fires only on EXTREME single-side
# concentration (|net|/gross > 0.80 <=> one side > ~90% of risk-bearing gross), to flag accidental
# stacking of correlated unpaired legs — NOT to pressure adding a hedge. The CIO may run a one-sided
# book by design, so the bar is high and the message is informational.
_TILT_WARN = 0.80


def _is_risk_bearing(p: Position) -> bool:
    """True when a stop-out still LOSES money: a long with stop below entry, or a short with stop
    above entry. A profit-locked / breakeven leg (trailed stop on the gain side of entry) has zero
    downside risk and is NOT a directional bet — it must not drive the neutrality nag."""
    return (p.direction == "long" and p.stop < p.entry) or \
           (p.direction == "short" and p.stop > p.entry)


def book_exposure(positions: list[Position], prices: dict[str, float], equity: float) -> dict:
    """Gross long $ vs gross short $ and the net directional tilt (notional = qty * mark, mark
    falling back to entry when missing). `tilt` = |net| / gross. ALSO computes the RISK-BEARING view
    (`*_rb`) counting only legs whose stop-out still loses money — a book that looks net-short by
    notional but is fully de-risked (all stops at/past breakeven) carries NO directional risk, and
    the nag keys off the risk-bearing tilt so it stays silent there.
    `long_share` = gross_long/gross."""
    gross_long = gross_short = gl_rb = gs_rb = 0.0
    n_long = n_short = 0
    for p in positions:
        mark = prices.get(p.symbol)
        notional = abs(p.qty) * float(mark if mark is not None else p.entry)
        rb = _is_risk_bearing(p)
        if p.direction == "long":
            gross_long += notional
            gl_rb += notional if rb else 0.0
            n_long += 1
        else:
            gross_short += notional
            gs_rb += notional if rb else 0.0
            n_short += 1
    gross, net = gross_long + gross_short, gross_long - gross_short
    gross_rb, net_rb = gl_rb + gs_rb, gl_rb - gs_rb
    return {
        "gross_long": round(gross_long, 2), "gross_short": round(gross_short, 2),
        "net": round(net, 2), "gross": round(gross, 2),
        "net_pct_equity": round(net / equity, 4) if equity else 0.0,
        "tilt": round(abs(net) / gross, 4) if gross > 0 else 0.0,
        "long_share": round(gross_long / gross, 4) if gross > 0 else 0.0,
        "n_long": n_long, "n_short": n_short,
        # risk-bearing view (drives the nag — profit-locked legs excluded)
        "gross_long_rb": round(gl_rb, 2), "gross_short_rb": round(gs_rb, 2),
        "net_rb": round(net_rb, 2), "gross_rb": round(gross_rb, 2),
        "net_rb_pct_equity": round(net_rb / equity, 4) if equity else 0.0,
        "tilt_rb": round(abs(net_rb) / gross_rb, 4) if gross_rb > 0 else 0.0,
    }


def exposure_warning(exposure: dict, tilt_warn: float = _TILT_WARN) -> str | None:
    """Directional-concentration TELEMETRY, keyed on RISK-BEARING exposure. Fires ONLY at extreme
    single-side concentration (tilt > tilt_warn, default 0.80) to flag accidental stacking of
    correlated unpaired legs. It does NOT pressure a hedge — this is an aggressive directional desk
    and a one-sided book is valid by design. Symmetric long/short; a flat, balanced, OR fully
    de-risked (stops past breakeven) book is silent. Falls back to raw notional if *_rb absent."""
    gross_rb = exposure.get("gross_rb", exposure.get("gross", 0.0))
    tilt_rb = exposure.get("tilt_rb", exposure.get("tilt", 0.0))
    if gross_rb <= 0 or tilt_rb <= tilt_warn:
        return None
    gl_rb = exposure.get("gross_long_rb", exposure.get("gross_long", 0.0))
    long_share = gl_rb / gross_rb if gross_rb > 0 else 0.0
    net_pct = exposure.get("net_rb_pct_equity", exposure.get("net_pct_equity", 0.0))
    side, share = ("LONG", long_share) if long_share >= 0.5 else ("SHORT", 1 - long_share)
    return (f"directional concentration HIGH: {share:.0%} of risk-bearing gross is {side}, net "
            f"{net_pct:+.0%} of equity — confirm this is a DELIBERATE regime bet, not accidental "
            f"stacking of correlated unpaired legs (telemetry only — one-sided books are valid).")


def portfolio_health(
    balance: float, peak_equity: float, positions: list[Position],
    prices: dict[str, float], recent_hit_rate: float = 0.5,
) -> PortfolioHealth:
    """Compute A1's PortfolioHealth from live marks, raising the high-water mark if exceeded."""
    equity = total_equity(balance, positions, prices)
    return PortfolioHealth(
        equity=equity,
        peak_equity=max(peak_equity, equity),
        open_heat=open_heat(positions, equity) if equity > 0 else 0.0,
        recent_hit_rate=recent_hit_rate,
    )
