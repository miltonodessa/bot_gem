"""
Main bot orchestrator.

Trading loop:
  1. Every NARRATIVE_REFRESH_MINUTES → refresh Twitter narrative
  2. Every 2 minutes → discover new tokens matching narrative
  3. For each candidate passing filters → evaluate entry
  4. Monitor open positions → execute exits per risk rules
  5. Log state continuously

Strategy summary (from wallet AF6sy... analysis):
  - Golden zone MC: $5k-$20k (79% WR)
  - Position: $100-$300 per trade (82% WR)
  - Hold target: 30min-2h (most profitable window)
  - Hard rules: 1 buy only, no DCA, exit at 6h max
  - Routing: Jupiter → pump.fun pools (74% of top trades)
  - Timing: UTC 4h, 13-15h optimal
"""

import asyncio
import signal
import sys
import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

try:
    from solders.keypair import Keypair
    import base58
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False
    logger.warning("solders not installed, wallet loading disabled")

from config import config
from twitter_scanner import TwitterNarrativeScanner, NarrativeReport
from token_discovery import TokenDiscovery, TokenCandidate
from jupiter_client import JupiterClient, SwapResult
from risk_manager import RiskManager, Position, ExitReason

console = Console()


def load_keypair() -> Optional["Keypair"]:
    """Load wallet keypair from env."""
    if not SOLDERS_AVAILABLE:
        return None
    pk = config.private_key
    if not pk:
        logger.warning("No PRIVATE_KEY set — running in wallet-less mode")
        return None
    try:
        decoded = base58.b58decode(pk)
        return Keypair.from_bytes(decoded)
    except Exception as e:
        logger.error(f"Failed to load keypair: {e}")
        return None


class NarrativeTradingBot:
    """
    Main bot class. Coordinates narrative scanning, token discovery,
    entry/exit decisions, and execution via Jupiter.
    """

    SCAN_INTERVAL_SECONDS = 120   # scan for new tokens every 2 min
    MONITOR_INTERVAL_SECONDS = 30  # check existing positions every 30s
    PRICE_REFRESH_SECONDS = 60     # refresh position prices every 60s

    def __init__(self):
        self.keypair = load_keypair()
        self.risk = RiskManager()
        self._running = False
        self._narrative: Optional[NarrativeReport] = None
        self._last_narrative_refresh = 0.0

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(self):
        """Start the bot. Runs until interrupted."""
        self._running = True
        self._setup_signal_handlers()

        mode = "DRY RUN" if config.dry_run else "LIVE"
        logger.info(f"Starting Narrative Trading Bot [{mode}]")

        if self.keypair:
            wallet_addr = str(self.keypair.pubkey())
            logger.info(f"Wallet: {wallet_addr}")
        else:
            logger.warning("No wallet loaded — all trades will be simulated")

        async with (
            TwitterNarrativeScanner() as twitter,
            TokenDiscovery() as discovery,
            JupiterClient(self.keypair) as jupiter,
        ):
            self._print_startup_banner()

            # Initial narrative scan
            self._narrative = await twitter.get_narrative(force_refresh=True)
            self._log_narrative(self._narrative)

            # Concurrent tasks
            tasks = [
                asyncio.create_task(self._narrative_loop(twitter)),
                asyncio.create_task(self._discovery_loop(twitter, discovery, jupiter)),
                asyncio.create_task(self._monitor_loop(discovery, jupiter)),
                asyncio.create_task(self._status_loop()),
            ]

            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                logger.info("Bot stopped. Closing open positions...")
                await self._emergency_exit_all(discovery, jupiter)

    # ── Task loops ────────────────────────────────────────────────────────────

    async def _narrative_loop(self, twitter: TwitterNarrativeScanner):
        """Periodically refresh the Twitter narrative."""
        while self._running:
            await asyncio.sleep(config.narrative_refresh_minutes * 60)
            try:
                self._narrative = await twitter.get_narrative(force_refresh=True)
                self._log_narrative(self._narrative)
            except Exception as e:
                logger.error(f"Narrative refresh error: {e}")

    async def _discovery_loop(
        self,
        twitter: TwitterNarrativeScanner,
        discovery: TokenDiscovery,
        jupiter: JupiterClient,
    ):
        """Continuously discover and evaluate new tokens."""
        while self._running:
            try:
                if self._narrative:
                    await self._scan_and_enter(twitter, discovery, jupiter)
            except Exception as e:
                logger.error(f"Discovery loop error: {e}")
            await asyncio.sleep(self.SCAN_INTERVAL_SECONDS)

    async def _monitor_loop(
        self,
        discovery: TokenDiscovery,
        jupiter: JupiterClient,
    ):
        """Monitor open positions and trigger exits."""
        while self._running:
            try:
                await self._check_exits(discovery, jupiter)
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(self.MONITOR_INTERVAL_SECONDS)

    async def _status_loop(self):
        """Print status every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            self._print_status()

    # ── Core trading logic ────────────────────────────────────────────────────

    async def _scan_and_enter(
        self,
        twitter: TwitterNarrativeScanner,
        discovery: TokenDiscovery,
        jupiter: JupiterClient,
    ):
        """Discover tokens and execute entries for top candidates."""
        if not self._narrative:
            return

        candidates = await discovery.discover(
            narrative=self._narrative,
            narrative_scanner=twitter,
            max_results=10,
        )

        if not candidates:
            logger.debug("No candidates found in this scan")
            return

        # Evaluate top candidates for entry
        sol_price = await jupiter.get_sol_price()

        for candidate in candidates[:3]:  # try top 3 at most
            can_enter, reason = self.risk.can_enter(
                mint=candidate.mint,
                narrative_score=candidate.narrative_score,
            )
            if not can_enter:
                logger.debug(f"Entry blocked for {candidate.symbol}: {reason}")
                continue

            logger.info(f"Entering: {candidate}")
            await self._execute_entry(candidate, jupiter, sol_price)

            # Only one new entry per scan cycle
            break

    async def _execute_entry(
        self,
        candidate: TokenCandidate,
        jupiter: JupiterClient,
        sol_price: float,
    ):
        """Buy a token candidate."""
        trade_sol = self.risk.position_size_sol(sol_price)
        trade_usd = trade_sol * sol_price

        logger.info(
            f"Buying {candidate.symbol} | "
            f"${trade_usd:.0f} ({trade_sol:.4f} SOL) | "
            f"MC=${candidate.market_cap_usd:,.0f} | "
            f"Narrative={self._narrative.dominant_narrative if self._narrative else 'N/A'}"
        )

        result: SwapResult = await jupiter.execute_buy(
            token_mint=candidate.mint,
            usd_amount=trade_usd,
            prefer_pump=candidate.is_pump_fun or config.prefer_pump_routing,
        )

        if result.success:
            self.risk.register_entry(
                mint=candidate.mint,
                symbol=candidate.symbol,
                entry_price_usd=candidate.price_usd,
                entry_sol=trade_sol,
                token_amount=result.out_amount,
                narrative=self._narrative.dominant_narrative if self._narrative else "",
                narrative_score=candidate.narrative_score,
                tx_signature=result.tx_signature,
            )
            logger.info(f"Buy confirmed: {candidate.symbol} | tx={result.tx_signature}")
        else:
            logger.warning(f"Buy failed for {candidate.symbol}: {result.error}")

    async def _check_exits(self, discovery: TokenDiscovery, jupiter: JupiterClient):
        """Check all open positions for exit signals."""
        if not self.risk.open_positions:
            return

        sol_price = await jupiter.get_sol_price()

        for mint, position in list(self.risk.open_positions.items()):
            # Refresh current price
            token_info = await discovery.get_token_info(mint)
            if token_info:
                position.current_price_usd = token_info.price_usd

            # Check exit conditions
            should_exit, reason = self.risk.should_exit(position)
            if should_exit:
                await self._execute_exit(position, reason, jupiter, sol_price)

    async def _execute_exit(
        self,
        position: Position,
        reason: ExitReason,
        jupiter: JupiterClient,
        sol_price: float,
    ):
        """Sell a position."""
        logger.info(
            f"Exiting {position.symbol} | "
            f"Reason={reason.value} | "
            f"PnL={position.pnl_pct:+.1f}%"
        )

        result: SwapResult = await jupiter.execute_sell(
            token_mint=position.mint,
            token_amount=position.token_amount,
            prefer_pump=config.prefer_pump_routing,
        )

        if result.success:
            # Calculate actual SOL PnL
            received_sol = result.out_amount / 1e9
            pnl_sol = received_sol - position.entry_sol - result.fee_sol
            self.risk.register_exit(position, reason, pnl_sol)
            logger.info(
                f"Sell confirmed: {position.symbol} | "
                f"PnL={pnl_sol:+.4f} SOL (${pnl_sol * sol_price:+.2f}) | "
                f"tx={result.tx_signature}"
            )
        else:
            logger.error(
                f"Sell FAILED for {position.symbol}: {result.error} — "
                f"position remains open!"
            )

    async def _emergency_exit_all(self, discovery: TokenDiscovery, jupiter: JupiterClient):
        """Close all open positions on shutdown."""
        if not self.risk.open_positions:
            return
        logger.warning(f"Emergency exit: {len(self.risk.open_positions)} positions")
        sol_price = await jupiter.get_sol_price()
        for mint, position in list(self.risk.open_positions.items()):
            await self._execute_exit(position, ExitReason.MANUAL, jupiter, sol_price)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _setup_signal_handlers(self):
        def _stop(sig, frame):
            logger.info(f"Signal {sig} received, stopping...")
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def _log_narrative(self, report: NarrativeReport):
        logger.info(
            f"Active narrative: {report.dominant_narrative} | "
            f"Trending tokens: {', '.join(report.trending_tokens[:10])}"
        )
        if report.narratives:
            ns = report.narratives[0]
            logger.info(
                f"  Score={ns.score:.2f} | "
                f"Keywords: {', '.join(ns.keywords_hit[:5])}"
            )

    def _print_status(self):
        status = self.risk.get_status()
        table = Table(title="Bot Status", show_header=True)
        table.add_column("Metric")
        table.add_column("Value")
        for k, v in status.items():
            if k != "positions":
                table.add_row(str(k), str(v))
        console.print(table)

        if self._narrative:
            console.print(
                Panel(
                    f"Narrative: [bold green]{self._narrative.dominant_narrative}[/]\n"
                    f"Top tokens: {', '.join(self._narrative.trending_tokens[:8])}",
                    title="Current Market Narrative",
                )
            )

    def _print_startup_banner(self):
        console.print(
            Panel(
                "[bold cyan]Solana Narrative Trading Bot[/]\n\n"
                f"Mode: [bold {'red' if not config.dry_run else 'yellow'}]"
                f"{'LIVE TRADING' if not config.dry_run else 'DRY RUN'}[/]\n"
                f"Golden Zone: ${config.min_market_cap:,.0f} – ${config.max_market_cap:,.0f} MC\n"
                f"Position: ${config.trade_amount_sol * 150:.0f} per trade\n"
                f"Stop Loss: {config.stop_loss_pct}% | TP: {config.take_profit_pct}%\n"
                f"Max Hold: {config.max_hold_hours}h",
                title="Startup",
            )
        )


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="7 days",
    )

    os.makedirs("logs", exist_ok=True)

    bot = NarrativeTradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
