"""
Central configuration for the Solana narrative trading bot.
Strategy based on wallet AF6syABApBp7d1NfjUkmKG7BBBEWVMvXadyvqQgjLnRN analysis.
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # ── Wallet / RPC ──────────────────────────────────────────────────────────
    private_key: str = os.getenv("PRIVATE_KEY", "")
    rpc_url: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    backup_rpc_url: str = os.getenv("BACKUP_RPC_URL", "")

    # ── Jupiter ───────────────────────────────────────────────────────────────
    jupiter_api_url: str = os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6")
    jupiter_price_api: str = os.getenv("JUPITER_PRICE_API", "https://price.jup.ag/v4")

    # ── Twitter / X ───────────────────────────────────────────────────────────
    twitter_bearer_token: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    twitter_api_key: str = os.getenv("TWITTER_API_KEY", "")
    twitter_api_secret: str = os.getenv("TWITTER_API_SECRET", "")
    twitter_access_token: str = os.getenv("TWITTER_ACCESS_TOKEN", "")
    twitter_access_secret: str = os.getenv("TWITTER_ACCESS_SECRET", "")

    # ── Data APIs ─────────────────────────────────────────────────────────────
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")
    dexscreener_api: str = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
    birdeye_api_key: str = os.getenv("BIRDEYE_API_KEY", "")

    # ── Position sizing (OPTIMAL from analysis: $100-$300, 82% WR) ────────────
    trade_amount_sol: float = float(os.getenv("TRADE_AMOUNT_SOL", "0.5"))
    max_position_size_sol: float = float(os.getenv("MAX_POSITION_SIZE_SOL", "1.0"))

    # ── Market cap filters (GOLDEN ZONE: $5k-$20k, 79% WR) ───────────────────
    min_market_cap: float = float(os.getenv("MIN_MARKET_CAP", "5000"))
    max_market_cap: float = float(os.getenv("MAX_MARKET_CAP", "20000"))
    max_extended_cap: float = float(os.getenv("MAX_EXTENDED_CAP", "500000"))

    # ── Risk management ───────────────────────────────────────────────────────
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "25"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "150"))
    max_hold_hours: float = float(os.getenv("MAX_HOLD_HOURS", "6"))
    target_hold_minutes: int = int(os.getenv("TARGET_HOLD_MINUTES", "90"))
    daily_loss_limit_sol: float = float(os.getenv("DAILY_LOSS_LIMIT_SOL", "2.0"))

    # ── Trade limits ──────────────────────────────────────────────────────────
    max_daily_trades: int = int(os.getenv("MAX_DAILY_TRADES", "20"))
    max_concurrent_positions: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
    max_slippage_bps: int = int(os.getenv("MAX_SLIPPAGE_BPS", "300"))

    # ── Execution behavior ────────────────────────────────────────────────────
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    narrative_refresh_minutes: int = int(os.getenv("NARRATIVE_REFRESH_MINUTES", "60"))

    # ── Optimal trading hours UTC (from wallet analysis) ─────────────────────
    # Best performance: 4h, 13h, 14h, 15h UTC
    # Worst: 6h, 8h, 12h, 22h UTC
    optimal_hours_utc: list = field(default_factory=lambda: [4, 13, 14, 15, 16, 17])
    avoid_hours_utc: list = field(default_factory=lambda: [6, 8, 12, 22, 23])

    # ── Token constants ───────────────────────────────────────────────────────
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # ── Strategy rules (from analysis) ────────────────────────────────────────
    # NEVER add to a position (1 buy = 79% WR, 2+ buys = 45% WR)
    allow_dca: bool = False
    # Prefer Jupiter → pump.fun routing (74% of winning trades)
    prefer_pump_routing: bool = True
    # Minimum narrative score to enter a trade
    min_narrative_score: float = 0.6
    # Minimum liquidity
    min_liquidity_usd: float = 3000.0


# ── Narrative keywords tracked on Twitter ─────────────────────────────────────
# These are re-scored daily based on Twitter trend velocity
BASE_NARRATIVE_CATEGORIES = {
    "AI_AGENTS": [
        "ai agent", "autonomous agent", "ai16z", "eliza framework",
        "agent protocol", "ai defi", "ai trading bot", "goat"
    ],
    "DEPIN": [
        "depin", "decentralized physical", "iotex", "helium", "render",
        "akash", "livepeer", "physical infrastructure"
    ],
    "MEME_META": [
        "dog wif hat", "meme season", "memecoin", "solana meme",
        "pump fun new", "1000x gem", "low cap gem"
    ],
    "GAMING": [
        "solana gaming", "play to earn", "gamefi solana", "nft gaming",
        "onchain game", "fully onchain"
    ],
    "RWA": [
        "real world asset", "tokenized", "rwa solana", "tokenized stocks",
        "real estate token", "commodity token"
    ],
    "LAUNCHPAD": [
        "new launch", "fair launch", "pump fun launch", "stealth launch",
        "presale", "token launch today"
    ],
    "TRENDING_MEME": [
        "trump", "elon", "viral", "trending", "1000x", "100x"
    ],
    "LAYER2_BRIDGE": [
        "solana bridge", "cross chain", "wormhole", "allbridge",
        "sol ecosystem"
    ],
}

# Twitter accounts to monitor for alpha/narratives
ALPHA_TWITTER_ACCOUNTS = [
    "solana", "pumpdotfun", "jupiterexchange", "raydium_io",
    "heliumsystems", "render_token", "HsakaTrades", "CryptoKaleo",
    "AltcoinSherpa", "WClementeIII", "inversebrah", "DegenSpartan",
    "gainzy222", "CryptoGodJohn", "Rewkang",
]

# Hashtags to monitor
TRENDING_HASHTAGS = [
    "#Solana", "#SOL", "#memecoin", "#pumpfun", "#depin",
    "#aiagent", "#web3", "#crypto", "#altcoin", "#gem",
    "#1000x", "#100x", "#newlisting", "#stealth"
]

config = TradingConfig()
