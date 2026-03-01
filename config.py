"""
Central configuration for the Solana narrative trading bot.
Strategy based on wallet AF6syABApBp7d1NfjUkmKG7BBBEWVMvXadyvqQgjLnRN analysis.
"""
import os
from dataclasses import dataclass, field
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
    # Best: 4h, 13h, 14h, 15h UTC  |  Worst: 6h, 8h, 12h, 22h UTC
    optimal_hours_utc: list = field(default_factory=lambda: [4, 13, 14, 15, 16, 17])
    avoid_hours_utc: list = field(default_factory=lambda: [6, 8, 12, 22, 23])

    # ── Token constants ───────────────────────────────────────────────────────
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # ── Strategy rules (from analysis) ────────────────────────────────────────
    allow_dca: bool = False          # NEVER add to position (2+ buys = 45% WR)
    prefer_pump_routing: bool = True  # Jupiter→pump.fun = 74% of winning trades
    min_narrative_score: float = 0.4  # lowered vs static categories (dynamic is noisier)
    min_liquidity_usd: float = 3000.0


# ── OG Solana memes (mint addresses) ─────────────────────────────────────────
# Tracked for revival signals: volume/price spike = OG meme season starting.
# When these pump → related new coins on pump.fun often follow.
OG_SOLANA_MEMES: dict[str, str] = {
    "BONK":   "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW":    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "BRETT":  "BRETTQszPe4F7GnMBFAGHMQNr9JMoqMaA8HNnRzsTroy",
    "BOME":   "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
    "MYRO":   "HhJpBhRRn4g56VsyLuT8DL5Bv31HkXqsrahTTUCZeZg4",
    "PONKE":  "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK31CR6ZDN",
    "SLERF":  "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7ByyfkZ1",
    "GIGA":   "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
    "MOODENG":"ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzc8yy",
    "PNUT":   "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",
    "GOAT":   "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump",
}

# ── Viral seed accounts (for narrative scanning) ──────────────────────────────
# Mix of: Solana alpha callers, viral meme accounts, news accounts, crypto influencers
VIRAL_SEED_ACCOUNTS = [
    # Solana ecosystem
    "pumpdotfun", "jupiterexchange", "solana", "raydium_io",
    # Crypto alpha / callers
    "HsakaTrades", "CryptoKaleo", "inversebrah", "DegenSpartan",
    "gainzy222", "CryptoGodJohn", "Rewkang", "AltcoinSherpa",
    # High-engagement general accounts (generate meme content)
    "elonmusk", "realDonaldTrump", "unusual_whales",
    # News / viral
    "BreakingNews", "disclosetv", "CollinRugg",
]

config = TradingConfig()
