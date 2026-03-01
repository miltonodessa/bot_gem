"""
Risk management module.

Enforces all risk rules derived from wallet analysis:

  ✓ Position size: $100–$300 (82% WR at this range)
  ✓ 1 buy per token — NO DCA (2+ buys drops WR from 79% → 45%)
  ✓ Hard stop-loss: -25%
  ✓ Take profit: +150% (2.5x)
  ✓ Max hold: 6 hours (>6h = disaster zone)
  ✓ Target exit window: 30–120 min (conviction window, most profitable)
  ✓ Daily loss circuit breaker
  ✓ Max concurrent positions: 3
  ✓ Optimal hour filter (UTC 4, 13–15)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from enum import Enum

from loguru import logger

from config import config


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_LIMIT = "time_limit"
    CONVICTION_WINDOW = "conviction_window"   # optional early exit at 30-120min
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_usd: float
    entry_sol: float              # SOL spent to buy
    token_amount: int             # base units held
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    buy_count: int = 1            # NEVER increment above 1 intentionally
    narrative: str = ""
    narrative_score: float = 0.0
    current_price_usd: float = 0.0
    tx_signature: Optional[str] = None

    @property
    def hold_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 60

    @property
    def hold_hours(self) -> float:
        return self.hold_minutes / 60

    @property
    def pnl_pct(self) -> float:
        if self.entry_price_usd <= 0 or self.current_price_usd <= 0:
            return 0.0
        return (self.current_price_usd - self.entry_price_usd) / self.entry_price_usd * 100

    @property
    def pnl_usd(self) -> float:
        """Rough USD PnL based on price change."""
        entry_usd = self.entry_sol * 150  # approximate, updated in bot loop
        return entry_usd * (self.pnl_pct / 100)

    @property
    def is_profitable(self) -> bool:
        return self.pnl_pct > 0

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} | entry=${self.entry_price_usd:.6f} "
            f"| now=${self.current_price_usd:.6f} | PnL={self.pnl_pct:+.1f}% "
            f"| held={self.hold_minutes:.0f}min)"
        )


@dataclass
class DailyStats:
    date: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    trades_executed: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_sol: float = 0.0
    total_pnl_usd: float = 0.0
    circuit_breaker_triggered: bool = False

    @property
    def winrate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def is_new_day(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.date != today


class RiskManager:
    """
    Central risk controller. All trading decisions must pass through here.
    """

    def __init__(self):
        self.open_positions: dict[str, Position] = {}  # mint → Position
        self.daily_stats = DailyStats()
        self.minted_today: set[str] = set()  # mints traded today (no re-entry)

    # ── Daily reset ───────────────────────────────────────────────────────────

    def _check_daily_reset(self):
        if self.daily_stats.is_new_day:
            logger.info(
                f"New day. Previous stats: {self.daily_stats.trades_executed} trades, "
                f"WR={self.daily_stats.winrate:.0%}, PnL={self.daily_stats.total_pnl_sol:+.4f} SOL"
            )
            self.daily_stats = DailyStats()
            self.minted_today = set()

    # ── Entry checks ──────────────────────────────────────────────────────────

    def can_enter(self, mint: str, narrative_score: float) -> tuple[bool, str]:
        """
        Returns (True, "") if it's safe to enter, or (False, reason) if blocked.
        """
        self._check_daily_reset()

        # Circuit breaker
        if self.daily_stats.circuit_breaker_triggered:
            return False, "circuit_breaker_active"

        # Daily loss limit
        if self.daily_stats.total_pnl_sol <= -config.daily_loss_limit_sol:
            self.daily_stats.circuit_breaker_triggered = True
            logger.warning("Daily loss limit hit — circuit breaker activated")
            return False, "daily_loss_limit"

        # Max daily trades
        if self.daily_stats.trades_executed >= config.max_daily_trades:
            return False, "max_daily_trades"

        # Max concurrent positions
        if len(self.open_positions) >= config.max_concurrent_positions:
            return False, f"max_concurrent_positions ({len(self.open_positions)})"

        # No re-entry same day
        if mint in self.minted_today:
            return False, "already_traded_today"

        # Already in this position
        if mint in self.open_positions:
            return False, "already_in_position"

        return True, ""

    def register_entry(
        self,
        mint: str,
        symbol: str,
        entry_price_usd: float,
        entry_sol: float,
        token_amount: int,
        narrative: str,
        narrative_score: float,
        tx_signature: Optional[str] = None,
    ) -> Position:
        pos = Position(
            mint=mint,
            symbol=symbol,
            entry_price_usd=entry_price_usd,
            entry_sol=entry_sol,
            token_amount=token_amount,
            narrative=narrative,
            narrative_score=narrative_score,
            current_price_usd=entry_price_usd,
            tx_signature=tx_signature,
        )
        self.open_positions[mint] = pos
        self.minted_today.add(mint)
        self.daily_stats.trades_executed += 1
        logger.info(f"Position opened: {pos}")
        return pos

    # ── Exit checks ───────────────────────────────────────────────────────────

    def should_exit(self, position: Position) -> tuple[bool, ExitReason]:
        """
        Evaluate whether to exit a position based on risk rules.
        Returns (should_exit, reason).
        """
        # Hard stop-loss (from config, default -25%)
        if position.pnl_pct <= -config.stop_loss_pct:
            logger.warning(f"STOP LOSS hit: {position}")
            return True, ExitReason.STOP_LOSS

        # Take profit (default +150% = 2.5x)
        if position.pnl_pct >= config.take_profit_pct:
            logger.info(f"TAKE PROFIT hit: {position}")
            return True, ExitReason.TAKE_PROFIT

        # Hard time limit (6h = disaster zone from analysis)
        if position.hold_hours >= config.max_hold_hours:
            logger.warning(f"TIME LIMIT hit ({position.hold_hours:.1f}h): {position}")
            return True, ExitReason.TIME_LIMIT

        # Conviction window exit: if 30-120min and in profit, consider exit
        # This is the most profitable window from wallet analysis
        if (
            config.target_hold_minutes / 3 <= position.hold_minutes <= config.target_hold_minutes
            and position.pnl_pct > 30  # at least +30% in the window
        ):
            logger.info(f"Conviction window exit opportunity: {position}")
            return True, ExitReason.CONVICTION_WINDOW

        return False, ExitReason.MANUAL

    def register_exit(
        self,
        position: Position,
        exit_reason: ExitReason,
        pnl_sol: float,
    ):
        mint = position.mint
        if mint in self.open_positions:
            del self.open_positions[mint]

        self.daily_stats.total_pnl_sol += pnl_sol
        if pnl_sol > 0:
            self.daily_stats.wins += 1
        else:
            self.daily_stats.losses += 1

        logger.info(
            f"Position closed: {position.symbol} | "
            f"Reason={exit_reason.value} | "
            f"PnL={pnl_sol:+.4f} SOL | "
            f"Hold={position.hold_minutes:.0f}min"
        )

    # ── State / reporting ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        self._check_daily_reset()
        return {
            "open_positions": len(self.open_positions),
            "daily_trades": self.daily_stats.trades_executed,
            "daily_winrate": f"{self.daily_stats.winrate:.0%}",
            "daily_pnl_sol": round(self.daily_stats.total_pnl_sol, 4),
            "circuit_breaker": self.daily_stats.circuit_breaker_triggered,
            "positions": [str(p) for p in self.open_positions.values()],
        }

    def position_size_sol(self, sol_price: float) -> float:
        """
        Return how much SOL to spend per trade.
        Targets $100–$300 range (82% WR zone from analysis).
        Caps at config.max_position_size_sol.
        """
        target_usd = (config.trade_amount_sol * sol_price)
        # Clamp to the optimal zone
        target_usd = max(100.0, min(300.0, target_usd))
        sol_amount = target_usd / sol_price
        return min(sol_amount, config.max_position_size_sol)
