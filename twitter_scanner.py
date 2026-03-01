"""
Twitter/X narrative scanner.

Strategy: The target wallet trades in clusters aligned with daily Twitter narratives.
This module detects which crypto narrative is currently trending and scores tokens against it.

Two modes:
  1. Tweepy (official API v2) — requires bearer token
  2. Fallback scraping via nitter/snscrape — no key needed
"""

import asyncio
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from loguru import logger

from config import (
    BASE_NARRATIVE_CATEGORIES,
    ALPHA_TWITTER_ACCOUNTS,
    TRENDING_HASHTAGS,
    config,
)


@dataclass
class NarrativeScore:
    category: str
    score: float          # 0.0 – 1.0
    keywords_hit: list
    tweet_count: int
    velocity: float       # tweets per hour
    top_accounts: list
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_fresh(self, max_age_minutes: int = 120) -> bool:
        age = datetime.now(timezone.utc) - self.updated_at
        return age < timedelta(minutes=max_age_minutes)


@dataclass
class NarrativeReport:
    """Aggregated daily narrative intelligence."""
    dominant_narrative: str
    narratives: list[NarrativeScore]
    trending_tokens: list[str]       # raw token symbols/names extracted from Twitter
    trending_mints: list[str]        # resolved Solana mint addresses (if any)
    raw_keywords: list[str]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def top_keywords(self, n: int = 20) -> list[str]:
        return self.raw_keywords[:n]

    def is_valid_hour_to_trade(self, optimal_hours: list, avoid_hours: list) -> bool:
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in avoid_hours:
            return False
        return current_hour in optimal_hours


class TwitterNarrativeScanner:
    """
    Scans Twitter/X for current crypto narratives.

    Priority:
      1. Official Tweepy API (if bearer token set)
      2. Nitter public instance scraping (fallback)
      3. Static keyword trending (last resort)
    """

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]

    def __init__(self):
        self._cache: Optional[NarrativeReport] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._use_api = bool(config.twitter_bearer_token)

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "SolanaTradeBot/1.0"}
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ── Public interface ──────────────────────────────────────────────────────

    async def get_narrative(self, force_refresh: bool = False) -> NarrativeReport:
        """Return current narrative report (cached for NARRATIVE_REFRESH_MINUTES)."""
        if self._cache and not force_refresh:
            if self._cache.narratives and self._cache.narratives[0].is_fresh(
                config.narrative_refresh_minutes
            ):
                logger.debug("Returning cached narrative report")
                return self._cache

        logger.info("Refreshing Twitter narrative scan...")
        report = await self._scan_narratives()
        self._cache = report
        return report

    async def score_token_against_narrative(
        self, token_name: str, token_symbol: str, narrative: NarrativeReport
    ) -> float:
        """
        Score how well a token matches the current narrative.
        Returns 0.0 – 1.0.
        """
        score = 0.0
        name_lower = token_name.lower()
        sym_lower = token_symbol.lower()
        keywords = narrative.top_keywords(30)

        # Direct name/symbol match in trending keywords
        for kw in keywords:
            if kw in name_lower or kw in sym_lower:
                score += 0.25
                break

        # Check against dominant narrative keywords
        dom_cat = BASE_NARRATIVE_CATEGORIES.get(narrative.dominant_narrative, [])
        for kw in dom_cat:
            if kw in name_lower or kw in sym_lower:
                score += 0.35
                break

        # Token appears explicitly in trending_tokens list
        if token_symbol.upper() in [t.upper() for t in narrative.trending_tokens]:
            score += 0.40

        return min(score, 1.0)

    # ── Internal scan logic ───────────────────────────────────────────────────

    async def _scan_narratives(self) -> NarrativeReport:
        if self._use_api:
            try:
                return await self._scan_via_twitter_api()
            except Exception as e:
                logger.warning(f"Twitter API failed ({e}), falling back to scraping")

        try:
            return await self._scan_via_nitter()
        except Exception as e:
            logger.warning(f"Nitter scraping failed ({e}), using static analysis")

        return self._static_narrative_report()

    async def _scan_via_twitter_api(self) -> NarrativeReport:
        """Use official Twitter API v2 search."""
        if not self._session:
            raise RuntimeError("Session not initialised")

        headers = {"Authorization": f"Bearer {config.twitter_bearer_token}"}
        keyword_counts: Counter = Counter()
        token_mentions: Counter = Counter()
        account_hits: dict = defaultdict(int)

        # Search recent tweets for each narrative category
        for category, keywords in BASE_NARRATIVE_CATEGORIES.items():
            query = " OR ".join(f'"{kw}"' for kw in keywords[:5])
            query += " lang:en -is:retweet"
            params = {
                "query": query,
                "max_results": 100,
                "tweet.fields": "created_at,author_id,public_metrics",
                "start_time": (
                    datetime.now(timezone.utc) - timedelta(hours=6)
                ).isoformat(),
            }
            try:
                async with self._session.get(
                    "https://api.twitter.com/2/tweets/search/recent",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tweets = data.get("data", [])
                        for tweet in tweets:
                            text = tweet.get("text", "").lower()
                            for kw in keywords:
                                if kw in text:
                                    keyword_counts[kw] += 1
                            extracted = self._extract_token_mentions(text)
                            for t in extracted:
                                token_mentions[t] += 1
                    elif resp.status == 429:
                        logger.warning("Twitter API rate limited, sleeping 15s")
                        await asyncio.sleep(15)
            except Exception as e:
                logger.debug(f"API search error for {category}: {e}")

        # Also search monitored accounts
        for account in ALPHA_TWITTER_ACCOUNTS[:5]:
            try:
                params = {
                    "query": f"from:{account}",
                    "max_results": 10,
                    "tweet.fields": "created_at,public_metrics",
                }
                async with self._session.get(
                    "https://api.twitter.com/2/tweets/search/recent",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tweet in data.get("data", []):
                            text = tweet.get("text", "").lower()
                            for token in self._extract_token_mentions(text):
                                token_mentions[token] += 1
                                account_hits[account] += 1
            except Exception:
                pass

        return self._build_report(keyword_counts, token_mentions, account_hits)

    async def _scan_via_nitter(self) -> NarrativeReport:
        """Scrape Nitter instances for trending crypto content."""
        if not self._session:
            raise RuntimeError("Session not initialised")

        keyword_counts: Counter = Counter()
        token_mentions: Counter = Counter()
        account_hits: dict = defaultdict(int)

        for instance in self.NITTER_INSTANCES:
            try:
                # Search trending crypto hashtags
                for hashtag in TRENDING_HASHTAGS[:6]:
                    tag = hashtag.lstrip("#")
                    url = f"{instance}/search?q=%23{tag}&f=tweets"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            texts = self._extract_text_from_html(html)
                            for text in texts:
                                text_lower = text.lower()
                                for cat_kws in BASE_NARRATIVE_CATEGORIES.values():
                                    for kw in cat_kws:
                                        if kw in text_lower:
                                            keyword_counts[kw] += 1
                                for token in self._extract_token_mentions(text_lower):
                                    token_mentions[token] += 1
                    await asyncio.sleep(0.5)

                # Scan alpha accounts
                for account in ALPHA_TWITTER_ACCOUNTS[:8]:
                    url = f"{instance}/{account}"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                            texts = self._extract_text_from_html(html)
                            for text in texts[:5]:
                                for token in self._extract_token_mentions(text.lower()):
                                    token_mentions[token] += 1
                                    account_hits[account] += 1
                    await asyncio.sleep(0.3)

                break  # success with this instance
            except Exception as e:
                logger.debug(f"Nitter {instance} failed: {e}")
                continue

        return self._build_report(keyword_counts, token_mentions, account_hits)

    def _static_narrative_report(self) -> NarrativeReport:
        """Last-resort fallback: use time-of-day heuristics."""
        hour = datetime.now(timezone.utc).hour
        # Morning EU session → AI narrative strong
        if 6 <= hour <= 12:
            dominant = "AI_AGENTS"
        # US session → Meme meta hot
        elif 13 <= hour <= 21:
            dominant = "MEME_META"
        else:
            dominant = "DEPIN"

        narratives = [
            NarrativeScore(
                category=dominant,
                score=0.5,
                keywords_hit=BASE_NARRATIVE_CATEGORIES[dominant][:3],
                tweet_count=0,
                velocity=0.0,
                top_accounts=[],
            )
        ]
        return NarrativeReport(
            dominant_narrative=dominant,
            narratives=narratives,
            trending_tokens=[],
            trending_mints=[],
            raw_keywords=BASE_NARRATIVE_CATEGORIES[dominant],
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_report(
        self,
        keyword_counts: Counter,
        token_mentions: Counter,
        account_hits: dict,
    ) -> NarrativeReport:
        """Convert raw counts into a structured NarrativeReport."""
        category_scores: dict[str, NarrativeScore] = {}

        for category, keywords in BASE_NARRATIVE_CATEGORIES.items():
            hits = {kw: keyword_counts.get(kw, 0) for kw in keywords}
            total = sum(hits.values())
            hit_keywords = [kw for kw, cnt in hits.items() if cnt > 0]
            score = min(total / max(len(keywords) * 5, 1), 1.0)

            if total > 0:
                category_scores[category] = NarrativeScore(
                    category=category,
                    score=score,
                    keywords_hit=hit_keywords,
                    tweet_count=total,
                    velocity=total / 6.0,  # per hour over 6h window
                    top_accounts=[a for a, c in account_hits.items() if c > 0],
                )

        # Sort by score descending
        sorted_narratives = sorted(
            category_scores.values(), key=lambda n: n.score, reverse=True
        )

        dominant = sorted_narratives[0].category if sorted_narratives else "MEME_META"

        # Top trending tokens from Twitter
        trending_tokens = [t.upper() for t, _ in token_mentions.most_common(30)]

        # All keywords sorted by count
        raw_keywords = [kw for kw, _ in keyword_counts.most_common(50)]

        logger.info(
            f"Narrative scan complete. Dominant: {dominant} "
            f"({len(sorted_narratives)} categories active)"
        )
        for ns in sorted_narratives[:3]:
            logger.info(f"  {ns.category}: score={ns.score:.2f}, tweets={ns.tweet_count}")

        return NarrativeReport(
            dominant_narrative=dominant,
            narratives=sorted_narratives,
            trending_tokens=trending_tokens,
            trending_mints=[],
            raw_keywords=raw_keywords,
        )

    @staticmethod
    def _extract_token_mentions(text: str) -> list[str]:
        """Extract potential token symbols from tweet text."""
        # Match $TOKEN patterns
        dollar_tickers = re.findall(r"\$([A-Z]{2,10})\b", text.upper())
        # Match standalone uppercase 2-8 char words that look like tickers
        bare_tickers = re.findall(r"\b([A-Z]{2,8})\b", text.upper())

        # Filter out common English words
        stop_words = {
            "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL",
            "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET",
            "HAS", "HIM", "HIS", "HOW", "ITS", "MAY", "NEW", "NOW",
            "OLD", "SEE", "TWO", "WAY", "WHO", "BOY", "DID", "GOT",
            "LET", "PUT", "SAY", "SHE", "TOO", "USE", "SOL", "BTC",
            "ETH", "USD", "ATH", "ATL", "LFG", "GG", "IMO", "FUD",
            "FOMO", "DEX", "CEX", "APR", "APY", "TVL", "RPC", "NFT",
            "DAO", "DeFi", "P2P", "P2E", "AI", "LLM"
        }

        candidates = list(dict.fromkeys(
            t for t in dollar_tickers + bare_tickers
            if t not in stop_words and len(t) >= 2
        ))
        return candidates[:20]

    @staticmethod
    def _extract_text_from_html(html: str) -> list[str]:
        """Very lightweight HTML text extraction (no BeautifulSoup dependency)."""
        # Remove script/style blocks
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Split into chunks (tweet-like segments)
        sentences = [s.strip() for s in text.split(".") if len(s.strip()) > 20]
        return sentences[:50]
