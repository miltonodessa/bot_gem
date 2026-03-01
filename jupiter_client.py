"""
Jupiter v6 DEX aggregator client.

Handles:
  - Quote fetching
  - Swap transaction building
  - Transaction signing & sending
  - Post-trade verification

The target wallet routes 74% of trades through Jupiter → pump.fun pools.
We replicate this by preferring pump.fun AMM in routing hints.
"""

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from loguru import logger
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

from config import config


@dataclass
class SwapQuote:
    input_mint: str
    output_mint: str
    in_amount: int          # lamports or token base units
    out_amount: int
    price_impact_pct: float
    route_plan: list
    other_amount_threshold: int
    swap_mode: str
    slippage_bps: int
    raw: dict               # full API response

    @property
    def price_impact_ok(self) -> bool:
        return self.price_impact_pct < 5.0  # reject >5% impact

    def __repr__(self) -> str:
        return (
            f"Quote(in={self.in_amount} → out={self.out_amount} | "
            f"impact={self.price_impact_pct:.2f}% | slippage={self.slippage_bps}bps)"
        )


@dataclass
class SwapResult:
    success: bool
    tx_signature: Optional[str]
    in_amount: int
    out_amount: int
    fee_sol: float
    error: Optional[str] = None

    def __repr__(self) -> str:
        if self.success:
            return f"SwapResult(OK | sig={self.tx_signature[:12]}... | in={self.in_amount} out={self.out_amount})"
        return f"SwapResult(FAIL | {self.error})"


class JupiterClient:
    """
    Async Jupiter v6 swap client with pump.fun routing preference.
    """

    # DEX labels used by Jupiter for routing hints
    PUMP_FUN_LABEL = "Pump.fun"
    RAYDIUM_LABEL = "Raydium"
    ORCA_LABEL = "Whirlpool"

    def __init__(self, keypair: Optional[Keypair] = None):
        self._keypair = keypair
        self._session: Optional[aiohttp.ClientSession] = None
        self._rpc: Optional[AsyncClient] = None
        self._sol_price_cache: float = 150.0
        self._sol_price_ts: float = 0

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"Accept": "application/json"},
        )
        self._rpc = AsyncClient(config.rpc_url)
        await self._refresh_sol_price()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()
        if self._rpc:
            await self._rpc.close()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_sol_price(self) -> float:
        """Return SOL/USD price, refreshed every 60s."""
        now = time.time()
        if now - self._sol_price_ts > 60:
            await self._refresh_sol_price()
        return self._sol_price_cache

    async def usd_to_lamports(self, usd_amount: float) -> int:
        """Convert dollar amount to SOL lamports."""
        sol_price = await self.get_sol_price()
        sol_amount = usd_amount / sol_price
        return int(sol_amount * 1e9)

    async def quote_buy(
        self,
        token_mint: str,
        usd_amount: float,
        prefer_pump: bool = True,
    ) -> Optional[SwapQuote]:
        """
        Get a buy quote: SOL → token.
        prefer_pump=True asks Jupiter to route via pump.fun AMM first.
        """
        lamports = await self.usd_to_lamports(usd_amount)
        return await self._get_quote(
            input_mint=config.sol_mint,
            output_mint=token_mint,
            amount=lamports,
            prefer_pump=prefer_pump,
        )

    async def quote_sell(
        self,
        token_mint: str,
        token_amount: int,
        prefer_pump: bool = True,
    ) -> Optional[SwapQuote]:
        """Get a sell quote: token → SOL."""
        return await self._get_quote(
            input_mint=token_mint,
            output_mint=config.sol_mint,
            amount=token_amount,
            prefer_pump=prefer_pump,
        )

    async def execute_buy(
        self,
        token_mint: str,
        usd_amount: float,
        prefer_pump: bool = True,
    ) -> SwapResult:
        """
        Execute a buy swap SOL → token.
        In DRY_RUN mode logs the intended trade without sending.
        """
        quote = await self.quote_buy(token_mint, usd_amount, prefer_pump)
        if not quote:
            return SwapResult(False, None, 0, 0, 0, "Failed to get quote")

        if not quote.price_impact_ok:
            return SwapResult(
                False, None, 0, 0, 0,
                f"Price impact too high: {quote.price_impact_pct:.1f}%"
            )

        if config.dry_run:
            logger.info(f"[DRY RUN] BUY {token_mint[:8]}... {quote}")
            return SwapResult(True, "DRY_RUN_SIG", quote.in_amount, quote.out_amount, 0.0)

        return await self._execute_swap(quote)

    async def execute_sell(
        self,
        token_mint: str,
        token_amount: int,
        prefer_pump: bool = True,
    ) -> SwapResult:
        """Execute a sell swap token → SOL."""
        quote = await self.quote_sell(token_mint, token_amount, prefer_pump)
        if not quote:
            return SwapResult(False, None, 0, 0, 0, "Failed to get sell quote")

        if config.dry_run:
            logger.info(f"[DRY RUN] SELL {token_mint[:8]}... {quote}")
            return SwapResult(True, "DRY_RUN_SIG", quote.in_amount, quote.out_amount, 0.0)

        return await self._execute_swap(quote)

    async def get_token_balance(self, wallet_pubkey: str, token_mint: str) -> int:
        """Return token balance in base units."""
        if not self._rpc:
            return 0
        try:
            resp = await self._rpc.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_pubkey),
                {"mint": Pubkey.from_string(token_mint)},
            )
            accounts = resp.value
            if not accounts:
                return 0
            for account in accounts:
                info = account.account.data
                if hasattr(info, "parsed"):
                    amount = info.parsed["info"]["tokenAmount"]["amount"]
                    return int(amount)
        except Exception as e:
            logger.debug(f"get_token_balance error: {e}")
        return 0

    async def get_sol_balance(self, wallet_pubkey: str) -> float:
        """Return SOL balance in SOL."""
        if not self._rpc:
            return 0.0
        try:
            resp = await self._rpc.get_balance(Pubkey.from_string(wallet_pubkey))
            return resp.value / 1e9
        except Exception as e:
            logger.debug(f"get_sol_balance error: {e}")
            return 0.0

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        prefer_pump: bool,
    ) -> Optional[SwapQuote]:
        if not self._session:
            return None

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(config.max_slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }

        # Routing preference: pump.fun first, then Raydium
        if prefer_pump and config.prefer_pump_routing:
            params["dexes"] = f"{self.PUMP_FUN_LABEL},{self.RAYDIUM_LABEL},{self.ORCA_LABEL}"

        try:
            url = f"{config.jupiter_api_url}/quote"
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Jupiter quote error: HTTP {resp.status}")
                    return None
                data = await resp.json()

            if "error" in data:
                logger.warning(f"Jupiter quote error: {data['error']}")
                return None

            return SwapQuote(
                input_mint=input_mint,
                output_mint=output_mint,
                in_amount=int(data.get("inAmount", 0)),
                out_amount=int(data.get("outAmount", 0)),
                price_impact_pct=float(data.get("priceImpactPct", 0)),
                route_plan=data.get("routePlan", []),
                other_amount_threshold=int(data.get("otherAmountThreshold", 0)),
                swap_mode=data.get("swapMode", "ExactIn"),
                slippage_bps=config.max_slippage_bps,
                raw=data,
            )
        except Exception as e:
            logger.error(f"Jupiter quote request failed: {e}")
            return None

    async def _execute_swap(self, quote: SwapQuote) -> SwapResult:
        """Build, sign, and send a Jupiter swap transaction."""
        if not self._keypair or not self._session or not self._rpc:
            return SwapResult(False, None, 0, 0, 0, "Client not fully initialised")

        # Step 1: Get swap transaction from Jupiter
        try:
            swap_payload = {
                "quoteResponse": quote.raw,
                "userPublicKey": str(self._keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }
            url = f"{config.jupiter_api_url}/swap"
            async with self._session.post(url, json=swap_payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return SwapResult(False, None, 0, 0, 0, f"Swap API error: {body[:200]}")
                swap_data = await resp.json()
        except Exception as e:
            return SwapResult(False, None, 0, 0, 0, f"Swap request failed: {e}")

        # Step 2: Deserialise, sign, and send
        try:
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)

            # Sign with our keypair
            tx.sign([self._keypair])

            # Send with confirmed commitment
            opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
            resp = await self._rpc.send_raw_transaction(
                bytes(tx),
                opts=opts,
            )
            sig = str(resp.value)
            logger.info(f"Transaction sent: {sig}")

            # Step 3: Confirm
            confirmed = await self._confirm_transaction(sig)
            if not confirmed:
                return SwapResult(False, sig, 0, 0, 0, "Transaction not confirmed")

            # Estimate fee (rough: 5000 lamports base + priority)
            fee_sol = 0.000005

            return SwapResult(
                success=True,
                tx_signature=sig,
                in_amount=quote.in_amount,
                out_amount=quote.out_amount,
                fee_sol=fee_sol,
            )
        except Exception as e:
            logger.error(f"Swap execution failed: {e}")
            return SwapResult(False, None, 0, 0, 0, str(e))

    async def _confirm_transaction(
        self, signature: str, max_retries: int = 30, delay: float = 1.5
    ) -> bool:
        """Poll for transaction confirmation."""
        if not self._rpc:
            return False
        for i in range(max_retries):
            try:
                resp = await self._rpc.get_signature_statuses([signature])
                statuses = resp.value
                if statuses and statuses[0]:
                    status = statuses[0]
                    if status.confirmation_status in ("confirmed", "finalized"):
                        if status.err is None:
                            return True
                        else:
                            logger.error(f"Transaction failed on-chain: {status.err}")
                            return False
            except Exception as e:
                logger.debug(f"Confirmation poll error ({i}): {e}")
            await asyncio.sleep(delay)
        logger.warning(f"Transaction not confirmed after {max_retries * delay:.0f}s")
        return False

    async def _refresh_sol_price(self):
        """Fetch SOL/USD from Jupiter price API."""
        if not self._session:
            return
        try:
            url = f"{config.jupiter_price_api}/price?ids={config.sol_mint}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price_data = data.get("data", {}).get(config.sol_mint, {})
                    price = float(price_data.get("price", 0))
                    if price > 0:
                        self._sol_price_cache = price
                        self._sol_price_ts = time.time()
                        logger.debug(f"SOL price refreshed: ${price:.2f}")
        except Exception as e:
            logger.debug(f"SOL price refresh failed: {e}")
