"""
Token discovery module.

Finds Solana tokens matching the current viral narrative in two modes:

  NEW tokens  — just launched on pump.fun / Raydium matching viral keywords
                Golden Zone MC $5k–$20k (79% WR), age 2–1440 min

  NARRATIVE OG — existing tokens (age >6h) on the same narrative that are
                 currently pumping (volume spike, price move up).
                 "OG" here means: any token that was ALREADY on this topic
                 before today's news hit — not a fixed list of famous memes.
                 Examples: an "Israel" token from 2 months ago, a "Trump" token
                 from last cycle. There are thousands of these across all topics.
                 They get a relaxed MC filter (extended zone allowed).

The OG detection replaces the old hardcoded OG_SOLANA_MEMES list.
Every viral event potentially has its own "OG" tokens — we discover them live.
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from loguru import logger

from config import config
from twitter_scanner import NarrativeReport


@dataclass
class NarrativeOGSignal:
    """
    An existing token that is pumping in response to today's viral narrative.
    NOT a fixed famous meme — any token matching the event keywords that
    shows momentum (volume spike + price move).
    """
    symbol: str
    mint: str
    matched_keyword: str          # which viral keyword it matched
    volume_spike_pct: float       # h1 vol vs hourly average (%)
    price_change_1h_pct: float
    age_hours: float              # how old is this token
    signal_strength: float        # 0–1

    @property
    def is_strong(self) -> bool:
        return self.signal_strength >= 0.55

    def __repr__(self) -> str:
        return (
            f"NarrativeOG({self.symbol} | kw='{self.matched_keyword}' "
            f"| +{self.price_change_1h_pct:.0f}% 1h | vol_spike={self.volume_spike_pct:.0f}% "
            f"| age={self.age_hours:.0f}h | strength={self.signal_strength:.2f})"
        )


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
    source: str                 # "pump_fun" | "raydium" | "dexscreener" | "narrative_og"
    narrative_score: float = 0.0
    entry_score: float = 0.0    # final composite score
    is_narrative_og: bool = False   # True = existing token pumping on this narrative
    og_signal: Optional["NarrativeOGSignal"] = None
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
    DEXSCREENER_LATEST_SOLANA = "https://api.dexscreener.com/latest/dex/pairs/solana"
    # pump.fun API — try multiple known endpoints (API changes frequently)
    PUMP_FUN_URLS = [
        "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false",
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC",
        "https://client-api-2.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC",
    ]
    PUMP_FUN_TRENDING_URLS = [
        "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC",
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC",
    ]
    BIRDEYE_TRENDING = "https://public-api.birdeye.so/defi/trending_tokens?chain=solana&limit=50"

    # Generic words from fallback event — useless as DexScreener search terms
    _FALLBACK_SKIP_WORDS = {"sol", "meme", "doge", "pepe", "frog", "cat", "dog",
                            "moon", "gem", "ape", "pump", "solana"}

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

        Two parallel tracks:
          1. NEW tokens — just launched, match narrative keywords
          2. NARRATIVE OGs — existing tokens pumping on the same narrative

        Returns list sorted by entry_score descending.
        """
        logger.info(f"Discovering tokens for narrative: {narrative.dominant_narrative}")

        # Gather from all sources concurrently (new tokens + narrative OGs)
        results = await asyncio.gather(
            self._fetch_pump_fun_latest(),
            self._fetch_pump_fun_trending(),
            self._fetch_dexscreener_latest_solana(),   # ← always works, no keywords needed
            self._fetch_dexscreener_narrative(narrative),
            self._fetch_birdeye_trending(),
            self._find_narrative_ogs(narrative),       # ← dynamic OG detection
            return_exceptions=True,
        )

        source_names = [
            "pump_fun_latest", "pump_fun_trending",
            "dexscreener_latest", "dexscreener_narrative",
            "birdeye", "narrative_ogs",
        ]
        all_candidates: list[TokenCandidate] = []
        og_signals: list[NarrativeOGSignal] = []

        for name, r in zip(source_names, results):
            if isinstance(r, Exception):
                logger.warning(f"Source [{name}] error: {r}")
            elif isinstance(r, tuple):
                # _find_narrative_ogs returns (candidates, signals)
                candidates, signals = r
                all_candidates.extend(candidates)
                og_signals.extend(signals)
            elif isinstance(r, list):
                all_candidates.extend(r)

        # Attach OG signals to narrative report so bot.py/risk can access them
        narrative.og_signals = og_signals
        if og_signals:
            strong = [s for s in og_signals if s.is_strong]
            logger.info(
                f"Narrative OG signals: {len(og_signals)} found, "
                f"{len(strong)} strong: {[s.symbol for s in strong]}"
            )

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
        """Fetch the newest tokens from pump.fun, trying multiple API endpoints."""
        if not self._session:
            return []
        for url in self.PUMP_FUN_URLS:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"pump.fun latest HTTP {resp.status} at {url}")
                        continue
                    coins = await resp.json()
                    if not isinstance(coins, list) or not coins:
                        continue
                    candidates = [c for coin in coins if (c := self._parse_pump_fun_coin(coin))]
                    logger.info(f"pump.fun latest: {len(candidates)} tokens from {url}")
                    return candidates
            except Exception as e:
                logger.warning(f"pump.fun latest error ({url}): {e}")
        return []

    async def _fetch_pump_fun_trending(self) -> list[TokenCandidate]:
        """Fetch currently most-traded tokens on pump.fun."""
        if not self._session:
            return []
        for url in self.PUMP_FUN_TRENDING_URLS:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"pump.fun trending HTTP {resp.status} at {url}")
                        continue
                    coins = await resp.json()
                    if not isinstance(coins, list) or not coins:
                        continue
                    candidates = [c for coin in coins if (c := self._parse_pump_fun_coin(coin))]
                    logger.info(f"pump.fun trending: {len(candidates)} tokens")
                    return candidates
            except Exception as e:
                logger.warning(f"pump.fun trending error ({url}): {e}")
        return []

    async def _fetch_dexscreener_latest_solana(self) -> list[TokenCandidate]:
        """
        Fetch the latest pairs on Solana directly from DexScreener.
        This is the most reliable source — no keywords, no API key, always works.
        Returns the 50 most recently updated Solana pairs.
        """
        if not self._session:
            return []
        try:
            async with self._session.get(
                self.DEXSCREENER_LATEST_SOLANA,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"DexScreener latest Solana HTTP {resp.status}")
                    return []
                data = await resp.json()
                pairs = data.get("pairs") or []
                candidates = [c for p in pairs if (c := self._parse_dexscreener_pair(p))]
                logger.info(f"DexScreener latest Solana: {len(candidates)} pairs fetched")
                return candidates
        except Exception as e:
            logger.warning(f"DexScreener latest Solana error: {e}")
            return []

    async def _fetch_dexscreener_narrative(
        self, narrative: NarrativeReport
    ) -> list[TokenCandidate]:
        """
        Search DexScreener using DYNAMIC keywords from today's viral events.

        For each viral event we use:
          1. The meme derivatives (predicted coin names from the event)
          2. Explicit $TICKER mentions from Twitter
          3. OG meme symbols if revival signal is active

        This means if "Israel war" is viral today, we search for
        ISRAEL, IDF, BIBI, GAZA, etc. on Solana DEXes.
        """
        if not self._session:
            return []

        candidates = []

        # Priority 1: meme derivatives from viral events (highest signal)
        search_terms: list[str] = []
        for event in getattr(narrative, "viral_events", [])[:3]:
            search_terms.extend(event.meme_derivatives[:5])

        # Priority 2: $TICKER mentions from Twitter
        search_terms.extend(narrative.trending_tokens[:8])

        # Priority 3: OG meme symbols with revival signals
        for sig in getattr(narrative, "og_signals", []):
            if sig.is_strong:
                search_terms.append(sig.symbol)

        # Priority 4: top raw keywords as fallback
        search_terms.extend(narrative.top_keywords(5))

        # Deduplicate while preserving priority order
        seen: set[str] = set()
        unique_terms: list[str] = []
        for term in search_terms:
            t = term.strip().lower()
            if t and t not in seen and len(t) >= 2:
                seen.add(t)
                unique_terms.append(t)

        # Skip generic fallback words — they produce irrelevant results
        useful_terms = [t for t in unique_terms if t not in self._FALLBACK_SKIP_WORDS and len(t) >= 3]

        if not useful_terms:
            logger.info("DexScreener narrative search: no specific keywords (fallback mode), skipping")
            return []

        for kw in useful_terms[:10]:
            try:
                url = self.DEXSCREENER_SEARCH.format(query=kw)
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        logger.warning(f"DexScreener search HTTP {resp.status} for '{kw}'")
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []
                    added = 0
                    for pair in pairs:
                        if pair.get("chainId") != "solana":
                            continue
                        c = self._parse_dexscreener_pair(pair)
                        if c:
                            candidates.append(c)
                            added += 1
                    if added:
                        logger.info(f"DexScreener search '{kw}': {added} Solana pairs")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(f"DexScreener search error for '{kw}': {e}")

        logger.info(f"DexScreener narrative search total: {len(candidates)} candidates")
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

    async def _find_narrative_ogs(
        self, narrative: NarrativeReport
    ) -> tuple[list[TokenCandidate], list[NarrativeOGSignal]]:
        """
        Dynamically discover EXISTING tokens that are pumping on today's narrative.

        These are "narrative OGs": tokens that were created before today's news
        but match the same keywords and are now moving. NOT a fixed list.

        For example:
          - "Israel" viral today → search DexScreener for tokens named ISRAEL,
            IDF, BIBI, NETANYAHU, GAZA that are >6h old and are pumping
          - "Trump tariffs" viral → find TRUMP, TARIFF, MAGA tokens with spikes
          - Any topic: the market has thousands of existing themed tokens

        Criteria for a "narrative OG":
          age > 6h           (established, not brand new)
          price_change_1h > 8%   OR   volume_spike > 200%
          liquidity > $3k    (real market)
          NOT in dead zone MC $20k–$100k

        Returns (candidates_list, og_signals_list).
        """
        if not self._session:
            return [], []

        candidates: list[TokenCandidate] = []
        signals: list[NarrativeOGSignal] = []

        # Use the top meme derivatives from each viral event
        og_keywords: list[tuple[str, str]] = []  # (keyword, source_event_topic)
        for event in getattr(narrative, "viral_events", [])[:3]:
            # Use only the most specific words (longer = more specific to this event)
            specific_kws = sorted(event.meme_derivatives[:8], key=len, reverse=True)
            for kw in specific_kws[:4]:
                if len(kw) >= 3:
                    og_keywords.append((kw, event.topic))

        # Also add explicit $TICKER mentions from Twitter
        for ticker in narrative.trending_tokens[:5]:
            og_keywords.append((ticker, "twitter_ticker"))

        # Deduplicate keywords
        seen_kws: set[str] = set()
        unique_kw_pairs = []
        for kw, topic in og_keywords:
            if kw.lower() not in seen_kws:
                seen_kws.add(kw.lower())
                unique_kw_pairs.append((kw, topic))

        now_ts_ms = datetime.now(timezone.utc).timestamp() * 1000

        for kw, source_topic in unique_kw_pairs[:10]:
            try:
                url = self.DEXSCREENER_SEARCH.format(query=kw)
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []

                    for pair in pairs:
                        if pair.get("chainId") != "solana":
                            continue

                        # ── Age check: must be >6h old to qualify as "OG" ──
                        pair_created = pair.get("pairCreatedAt", 0) or 0
                        age_minutes = (now_ts_ms - pair_created) / 60_000 if pair_created else 0
                        age_hours = age_minutes / 60

                        if age_hours < 6:
                            continue   # too new = regular new launch, not OG

                        # ── Parse basic token data ──
                        base = pair.get("baseToken", {})
                        mint = base.get("address", "")
                        if not mint:
                            continue

                        price_change_1h = float((pair.get("priceChange") or {}).get("h1", 0))
                        vol_h1 = float((pair.get("volume") or {}).get("h1", 0))
                        vol_h6 = float((pair.get("volume") or {}).get("h6", 0))
                        vol_h24 = float((pair.get("volume") or {}).get("h24", 0))
                        liq = float((pair.get("liquidity") or {}).get("usd", 0))
                        mc = float(pair.get("fdv", 0) or pair.get("marketCap", 0) or 0)

                        if liq < config.min_liquidity_usd:
                            continue

                        # Skip dead zone MC ($20k–$100k) for OGs too
                        if 20_000 < mc < 100_000:
                            continue

                        # ── Momentum check: is it actually pumping? ──
                        # Compare last hour vs the 6h hourly average
                        avg_hourly_vol = vol_h6 / 6 if vol_h6 > 0 else (vol_h24 / 24 if vol_h24 > 0 else 0)
                        if avg_hourly_vol > 0:
                            vol_spike_pct = (vol_h1 - avg_hourly_vol) / avg_hourly_vol * 100
                        else:
                            vol_spike_pct = 0.0

                        # Must show at least one of: price move OR volume spike
                        has_momentum = (
                            price_change_1h >= 8.0       # price up 8%+ in 1h
                            or vol_spike_pct >= 200.0    # volume 3x+ hourly avg
                        )
                        if not has_momentum:
                            continue

                        # ── Compute signal strength ──
                        price_component = min(max(price_change_1h, 0) / 40, 1.0)  # 40% = full
                        vol_component = min(max(vol_spike_pct, 0) / 400, 1.0)     # 400% = full
                        strength = price_component * 0.55 + vol_component * 0.45

                        signal = NarrativeOGSignal(
                            symbol=base.get("symbol", ""),
                            mint=mint,
                            matched_keyword=kw,
                            volume_spike_pct=vol_spike_pct,
                            price_change_1h_pct=price_change_1h,
                            age_hours=age_hours,
                            signal_strength=strength,
                        )
                        signals.append(signal)
                        logger.debug(f"Narrative OG found: {signal}")

                        # Build TokenCandidate for this OG token
                        price_usd = float(pair.get("priceUsd", 0) or 0)
                        is_pump = "pump" in (pair.get("dexId", "") + pair.get("url", "")).lower()

                        c = TokenCandidate(
                            mint=mint,
                            symbol=base.get("symbol", ""),
                            name=base.get("name", ""),
                            market_cap_usd=mc,
                            liquidity_usd=liq,
                            price_usd=price_usd,
                            volume_24h=vol_h24,
                            age_minutes=age_minutes,
                            holder_count=0,
                            is_pump_fun=is_pump,
                            source="narrative_og",
                            is_narrative_og=True,
                            og_signal=signal,
                        )
                        candidates.append(c)

                await asyncio.sleep(0.25)

            except Exception as e:
                logger.debug(f"Narrative OG search error for '{kw}': {e}")

        # Deduplicate by mint
        seen_mints: set[str] = set()
        unique_candidates = []
        unique_signals = []
        seen_sig_mints: set[str] = set()

        for c in candidates:
            if c.mint not in seen_mints:
                seen_mints.add(c.mint)
                unique_candidates.append(c)

        for s in signals:
            if s.mint not in seen_sig_mints:
                seen_sig_mints.add(s.mint)
                unique_signals.append(s)

        # Sort signals by strength
        unique_signals.sort(key=lambda s: s.signal_strength, reverse=True)

        if unique_candidates:
            logger.info(f"Narrative OGs discovered: {len(unique_candidates)} tokens pumping on '{narrative.dominant_narrative}'")

        return unique_candidates, unique_signals

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
        """
        Hard filters — two different paths for new tokens vs narrative OGs.

        NEW tokens: strict age + MC golden zone
        NARRATIVE OGs: already pre-filtered in _find_narrative_ogs (momentum
                       check is their entry gate), so they only need MC check here.
        """
        # Minimum liquidity always required
        if not c.passes_liquidity:
            return False

        # Avoid the dead zone $20k–$100k for both paths (58% WR, net negative)
        if 20_000 < c.market_cap_usd < 100_000:
            return False

        if c.is_narrative_og:
            # OGs already checked age (>6h) and momentum inside _find_narrative_ogs
            # Here we just verify MC: golden or extended zone
            return c.in_golden_zone or c.in_extended_zone

        # NEW tokens: must be in golden zone or extended, and right age window
        if not c.in_golden_zone and not c.in_extended_zone:
            return False

        # Reject tokens < 2 min (rug risk) or > 24h for new launches
        if c.age_minutes < 2 or c.age_minutes > 1440:
            return False

        return True

    def _compute_entry_score(
        self, c: TokenCandidate, narrative: NarrativeReport
    ) -> float:
        """
        Composite entry score 0.0–1.0.

        NEW token weights:
          - Narrative alignment:           40%
          - Market cap in golden zone:     25%
          - Liquidity quality:             15%
          - Age sweetspot (5-60 min):      10%
          - Pump.fun routing bonus:        10%

        NARRATIVE OG weights (existing pumping token):
          - Signal strength from OG check: 45%
          - Narrative keyword match:       35%
          - Market cap (extended ok):      20%
          (Age/pump bonus not relevant for established tokens)
        """
        if c.is_narrative_og and c.og_signal:
            return self._score_narrative_og(c)

        # ── New token scoring ──────────────────────────────────────────────
        score = 0.0

        # 1. Narrative alignment (40%)
        score += c.narrative_score * 0.40

        # Bonus: matches top viral event
        top_events = getattr(narrative, "viral_events", [])
        if top_events and top_events[0].engagement_score >= 0.7:
            name_l = c.name.lower()
            sym_l = c.symbol.lower()
            for kw in top_events[0].meme_derivatives[:10]:
                if kw in name_l or kw in sym_l or name_l in kw or sym_l in kw:
                    score += 0.15
                    break

        # 2. Market cap score (25%)
        if c.in_golden_zone:
            mc_norm = 1.0 - (c.market_cap_usd - config.min_market_cap) / (
                config.max_market_cap - config.min_market_cap
            )
            score += max(mc_norm, 0) * 0.25
        elif c.in_extended_zone:
            score += 0.10

        # 3. Liquidity (15%)
        score += min(c.liquidity_usd / 15_000, 1.0) * 0.15

        # 4. Age sweetspot 5–60 min (10%)
        if 5 <= c.age_minutes <= 60:
            score += 0.10
        elif c.age_minutes < 5:
            score += 0.03

        # 5. pump.fun routing bonus (10%)
        if c.is_pump_fun:
            score += 0.10

        return min(score, 1.0)

    def _score_narrative_og(self, c: TokenCandidate) -> float:
        """
        Score for a narrative OG token (existing token pumping on today's narrative).

        Primary driver is momentum signal strength, secondary is narrative match.
        MC scoring is more lenient — OGs often have higher caps.
        """
        score = 0.0
        sig = c.og_signal

        # 1. Momentum signal strength (45%)
        score += sig.signal_strength * 0.45

        # 2. Narrative keyword match (35%)
        score += c.narrative_score * 0.35

        # 3. Market cap (20%) — golden zone best, extended ok
        if c.in_golden_zone:
            mc_norm = 1.0 - (c.market_cap_usd - config.min_market_cap) / (
                config.max_market_cap - config.min_market_cap
            )
            score += max(mc_norm, 0) * 0.20
        elif c.in_extended_zone:
            # Still valuable if momentum is real
            score += 0.12

        return min(score, 1.0)
