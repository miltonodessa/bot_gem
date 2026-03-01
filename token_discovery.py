"""
Token discovery module.

Finds new Solana tokens on pump.fun / Raydium that match the current Twitter
narrative and pass the market-cap / liquidity filters from wallet analysis:

  Golden Zone:  MC $5k – $20k   → 79% WR, best absolute PnL
  Avoid zone:   MC $20k – $100k → 58% WR, net negative
  Extended:     MC $100k – $500k → 60% WR (only with strong narrative conviction)

Preferred routing: Jupiter → pump.fun pools (74% of winning trades).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from loguru import logger

from config import config
from twitter_scanner import NarrativeReport


@dataclass
class TokenCandidate:
    mint: str
    symbol: str
    name: str
    market_cap_usd: float
    liquidity_usd: float
    price_usd: float
    volume_24h: float
    age_minutes: float          # how old is the pool
    holder_count: int
    is_pump_fun: bool
    source: str                 # "pump_fun" | "raydium" | "dexscreener"
    narrative_score: float = 0.0
    entry_score: float = 0.0    # final composite score
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def in_golden_zone(self) -> bool:
        return config.min_market_cap <= self.market_cap_usd <= config.max_market_cap

    @property
    def in_extended_zone(self) -> bool:
        return config.max_market_cap < self.market_cap_usd <= config.max_extended_cap

    @property
    def passes_liquidity(self) -> bool:
        return self.liquidity_usd >= config.min_liquidity_usd

    @property
    def is_tradeable(self) -> bool:
        return self.in_golden_zone and self.passes_liquidity

    def __repr__(self) -> str:
        zone = "GOLD" if self.in_golden_zone else ("EXT" if self.in_extended_zone else "OUT")
        return (
            f"Token({self.symbol} | MC=${self.market_cap_usd:,.0f} [{zone}] "
            f"| Liq=${self.liquidity_usd:,.0f} | score={self.entry_score:.2f})"
        )


class TokenDiscovery:
    """
    Multi-source token scanner:
      1. DexScreener /latest  – pump.fun + Raydium new pairs
      2. pump.fun API         – fresh bonding-curve tokens
      3. Birdeye trending     – narrative-aligned trending tokens (optional)
    """

    DEXSCREENER_NEW_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
    DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={query}"
    DEXSCREENER_NEW = "https://api.dexscreener.com/latest/dex/pairs/solana"
    PUMP_FUN_LATEST = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false"
    PUMP_FUN_TRENDING = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC"
    BIRDEYE_TRENDING = "https://public-api.birdeye.so/defi/trending_tokens?chain=solana&limit=50"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            }
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────────────────

    async def discover(
        self,
        narrative: NarrativeReport,
        narrative_scanner,
        max_results: int = 20,
    ) -> list[TokenCandidate]:
        """
        Discover and rank token candidates against the current narrative.
        Returns list sorted by entry_score descending.
        """
        logger.info(f"Discovering tokens for narrative: {narrative.dominant_narrative}")

        # Gather from all sources concurrently
        results = await asyncio.gather(
            self._fetch_pump_fun_latest(),
            self._fetch_pump_fun_trending(),
            self._fetch_dexscreener_narrative(narrative),
            self._fetch_birdeye_trending(),
            return_exceptions=True,
        )

        all_candidates: list[TokenCandidate] = []
        for r in results:
            if isinstance(r, Exception):
                logger.debug(f"Source error: {r}")
            elif isinstance(r, list):
                all_candidates.extend(r)

        # Deduplicate by mint
        seen = set()
        unique: list[TokenCandidate] = []
        for c in all_candidates:
            if c.mint not in seen:
                seen.add(c.mint)
                unique.append(c)

        logger.info(f"Raw candidates before filter: {len(unique)}")

        # Apply filters
        filtered = [c for c in unique if self._passes_hard_filters(c)]
        logger.info(f"Candidates after hard filters: {len(filtered)}")

        # Score each candidate against narrative
        for candidate in filtered:
            narrative_score = await narrative_scanner.score_token_against_narrative(
                candidate.name, candidate.symbol, narrative
            )
            candidate.narrative_score = narrative_score
            candidate.entry_score = self._compute_entry_score(candidate, narrative)

        # Sort by entry score
        filtered.sort(key=lambda c: c.entry_score, reverse=True)

        top = filtered[:max_results]
        for c in top[:5]:
            logger.info(f"  Candidate: {c}")

        return top

    async def get_token_info(self, mint: str) -> Optional[TokenCandidate]:
        """Fetch current on-chain data for a specific token mint."""
        if not self._session:
            return None
        try:
            url = self.DEXSCREENER_NEW_PAIRS.format(mint=mint)
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                if not pairs:
                    return None
                # Best pair = highest liquidity on Solana
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    return None
                pair = max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0)))
                return self._parse_dexscreener_pair(pair)
        except Exception as e:
            logger.debug(f"get_token_info error for {mint}: {e}")
            return None

    # ── Data fetchers ─────────────────────────────────────────────────────────

    async def _fetch_pump_fun_latest(self) -> list[TokenCandidate]:
        """Fetch the newest tokens from pump.fun."""
        if not self._session:
            return []
        try:
            async with self._session.get(self.PUMP_FUN_LATEST) as resp:
                if resp.status != 200:
                    return []
                coins = await resp.json()
                candidates = []
                for coin in coins:
                    c = self._parse_pump_fun_coin(coin)
                    if c:
                        candidates.append(c)
                logger.debug(f"pump.fun latest: {len(candidates)} tokens fetched")
                return candidates
        except Exception as e:
            logger.debug(f"pump.fun latest error: {e}")
            return []

    async def _fetch_pump_fun_trending(self) -> list[TokenCandidate]:
        """Fetch currently most-traded tokens on pump.fun."""
        if not self._session:
            return []
        try:
            async with self._session.get(self.PUMP_FUN_TRENDING) as resp:
                if resp.status != 200:
                    return []
                coins = await resp.json()
                candidates = []
                for coin in coins:
                    c = self._parse_pump_fun_coin(coin)
                    if c:
                        candidates.append(c)
                logger.debug(f"pump.fun trending: {len(candidates)} tokens fetched")
                return candidates
        except Exception as e:
            logger.debug(f"pump.fun trending error: {e}")
            return []

    async def _fetch_dexscreener_narrative(
        self, narrative: NarrativeReport
    ) -> list[TokenCandidate]:
        """Search DexScreener for tokens matching the narrative keywords."""
        if not self._session:
            return []
        candidates = []
        keywords = narrative.top_keywords(5)

        # Also add explicitly trending token symbols from Twitter
        keywords += narrative.trending_tokens[:5]

        for kw in keywords[:8]:
            try:
                url = self.DEXSCREENER_SEARCH.format(query=kw)
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []
                    for pair in pairs:
                        if pair.get("chainId") != "solana":
                            continue
                        c = self._parse_dexscreener_pair(pair)
                        if c:
                            candidates.append(c)
                await asyncio.sleep(0.2)  # rate limit
            except Exception as e:
                logger.debug(f"DexScreener search error for '{kw}': {e}")

        logger.debug(f"DexScreener narrative search: {len(candidates)} candidates")
        return candidates

    async def _fetch_birdeye_trending(self) -> list[TokenCandidate]:
        """Fetch Birdeye trending tokens (requires API key)."""
        if not self._session or not config.birdeye_api_key:
            return []
        try:
            headers = {"X-API-KEY": config.birdeye_api_key}
            async with self._session.get(self.BIRDEYE_TRENDING, headers=headers) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                tokens = data.get("data", {}).get("tokens", [])
                candidates = []
                for t in tokens:
                    c = self._parse_birdeye_token(t)
                    if c:
                        candidates.append(c)
                logger.debug(f"Birdeye trending: {len(candidates)} tokens")
                return candidates
        except Exception as e:
            logger.debug(f"Birdeye trending error: {e}")
            return []

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_pump_fun_coin(self, coin: dict) -> Optional[TokenCandidate]:
        try:
            mint = coin.get("mint", "")
            if not mint:
                return None

            # pump.fun uses bonding curve MC = price * 1_000_000_000 total supply
            usd_market_cap = float(coin.get("usd_market_cap", 0))
            virtual_sol_reserves = float(coin.get("virtual_sol_reserves", 0))
            virtual_token_reserves = float(coin.get("virtual_token_reserves", 1))

            # Estimate price from reserves
            price_usd = 0.0
            if virtual_token_reserves > 0:
                # sol_price_usd is not available here, use market cap / supply
                price_usd = usd_market_cap / 1_000_000_000 if usd_market_cap > 0 else 0

            # Estimate liquidity from SOL reserves (rough: 1 SOL ≈ $150)
            sol_price_approx = 150.0
            liquidity_usd = (virtual_sol_reserves / 1e9) * sol_price_approx

            created_ts = coin.get("created_timestamp", 0) / 1000
            now_ts = datetime.now(timezone.utc).timestamp()
            age_minutes = (now_ts - created_ts) / 60 if created_ts else 0

            return TokenCandidate(
                mint=mint,
                symbol=coin.get("symbol", ""),
                name=coin.get("name", ""),
                market_cap_usd=usd_market_cap,
                liquidity_usd=liquidity_usd,
                price_usd=price_usd,
                volume_24h=float(coin.get("volume_24h", 0)),
                age_minutes=age_minutes,
                holder_count=int(coin.get("holder_count", 0)),
                is_pump_fun=True,
                source="pump_fun",
            )
        except Exception as e:
            logger.debug(f"pump.fun parse error: {e}")
            return None

    def _parse_dexscreener_pair(self, pair: dict) -> Optional[TokenCandidate]:
        try:
            base = pair.get("baseToken", {})
            mint = base.get("address", "")
            if not mint:
                return None

            mc = float(pair.get("fdv", 0) or pair.get("marketCap", 0) or 0)
            liq = float((pair.get("liquidity") or {}).get("usd", 0))
            price = float(pair.get("priceUsd", 0) or 0)
            vol24 = float((pair.get("volume") or {}).get("h24", 0))

            pair_created = pair.get("pairCreatedAt", 0)
            now_ts = datetime.now(timezone.utc).timestamp() * 1000
            age_minutes = (now_ts - pair_created) / 60000 if pair_created else 0

            dex = pair.get("dexId", "")
            is_pump = "pump" in dex.lower() or "pump" in (pair.get("url", "")).lower()

            return TokenCandidate(
                mint=mint,
                symbol=base.get("symbol", ""),
                name=base.get("name", ""),
                market_cap_usd=mc,
                liquidity_usd=liq,
                price_usd=price,
                volume_24h=vol24,
                age_minutes=age_minutes,
                holder_count=0,
                is_pump_fun=is_pump,
                source="dexscreener",
            )
        except Exception as e:
            logger.debug(f"DexScreener parse error: {e}")
            return None

    def _parse_birdeye_token(self, token: dict) -> Optional[TokenCandidate]:
        try:
            mint = token.get("address", "")
            if not mint:
                return None
            return TokenCandidate(
                mint=mint,
                symbol=token.get("symbol", ""),
                name=token.get("name", ""),
                market_cap_usd=float(token.get("mc", 0)),
                liquidity_usd=float(token.get("liquidity", 0)),
                price_usd=float(token.get("v24hUSD", 0)),
                volume_24h=float(token.get("v24hUSD", 0)),
                age_minutes=0,
                holder_count=0,
                is_pump_fun=False,
                source="birdeye",
            )
        except Exception as e:
            logger.debug(f"Birdeye parse error: {e}")
            return None

    # ── Scoring / filtering ───────────────────────────────────────────────────

    def _passes_hard_filters(self, c: TokenCandidate) -> bool:
        """Hard filters based on wallet analysis golden rules."""
        # Must be in golden zone or extended zone (not $20k-$100k dead zone)
        in_golden = c.in_golden_zone
        in_extended = c.in_extended_zone
        if not in_golden and not in_extended:
            return False

        # Avoid the dead zone $20k–$100k (58% WR, net negative in analysis)
        if 20_000 < c.market_cap_usd < 100_000:
            return False

        # Minimum liquidity
        if not c.passes_liquidity:
            return False

        # Reject tokens < 2 min old (rug risk) or > 24h (missed move)
        if c.age_minutes < 2 or c.age_minutes > 1440:
            return False

        return True

    def _compute_entry_score(
        self, c: TokenCandidate, narrative: NarrativeReport
    ) -> float:
        """
        Composite entry score 0.0–1.0.

        Weights:
          - Narrative alignment: 40%
          - Market cap position within golden zone: 25%
          - Liquidity quality: 15%
          - Age sweetspot (5-60 min): 10%
          - Pump.fun routing bonus: 10%
        """
        score = 0.0

        # 1. Narrative alignment (40%)
        score += c.narrative_score * 0.40

        # 2. Market cap score (25%) — closer to $5k–$10k = better
        if c.in_golden_zone:
            # Normalise within golden zone: $5k=1.0, $20k=0.5
            mc_norm = 1.0 - (c.market_cap_usd - config.min_market_cap) / (
                config.max_market_cap - config.min_market_cap
            )
            score += max(mc_norm, 0) * 0.25
        elif c.in_extended_zone:
            score += 0.10  # partial credit for extended zone

        # 3. Liquidity (15%)
        liq_score = min(c.liquidity_usd / 15_000, 1.0)
        score += liq_score * 0.15

        # 4. Age sweetspot 5–60 min (10%)
        if 5 <= c.age_minutes <= 60:
            score += 0.10
        elif c.age_minutes < 5:
            score += 0.03  # very new = higher risk

        # 5. pump.fun routing bonus (10%)
        if c.is_pump_fun:
            score += 0.10

        return min(score, 1.0)
