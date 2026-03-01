"""
Twitter/X viral news narrative scanner.

Core idea:
  Solana memes are driven by WORLD EVENTS and VIRAL CONTENT — not sector categories.
  Today: Israel/Gaza war news → "BIBI", "IDF", "GAZA" tokens appear on pump.fun.
  Tomorrow: Elon posts something viral → dog/space/Mars memes pump.
  Day after: OG memes (BONK, WIF, POPCAT) start reviving because of general Solana hype.

This scanner:
  1. Detects what is VIRALLY TRENDING right now on Twitter/X (by engagement, not category)
  2. Extracts dynamic keywords from the "story of the day"
  3. Generates meme-name derivatives (the words that become coin names)
  4. Detects OG Solana meme revivals via volume/mention spikes
  5. Returns a NarrativeReport with dynamic keywords (no fixed categories)

Sources (priority order):
  1. Twitter API v2 — top tweets by impression count in last 2h
  2. Nitter scraping — engagement proxy via like/retweet counts
  3. Trending topics from public APIs (trends24.in etc.)
"""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from loguru import logger

from config import config, VIRAL_SEED_ACCOUNTS


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ViralEvent:
    """A single viral story / topic detected on Twitter."""
    topic: str                    # Human-readable label, e.g. "Israel war news"
    raw_keywords: list[str]       # Words extracted from viral tweets
    meme_derivatives: list[str]   # Predicted meme-coin name candidates
    engagement_score: float       # Normalised 0–1 (views + likes + RTs)
    tweet_count: int              # Number of viral tweets on this topic
    velocity: float               # Tweets/hour gaining momentum
    sample_tweets: list[str]      # Up to 3 tweet snippets
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"ViralEvent('{self.topic}' | score={self.engagement_score:.2f} "
            f"| derivatives={self.meme_derivatives[:5]})"
        )


@dataclass
class NarrativeScore:
    category: str
    score: float
    keywords_hit: list
    tweet_count: int
    velocity: float
    top_accounts: list
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_fresh(self, max_age_minutes: int = 120) -> bool:
        return (datetime.now(timezone.utc) - self.updated_at) < timedelta(minutes=max_age_minutes)


@dataclass
class NarrativeReport:
    """
    Dynamic narrative report — driven by what's actually viral today.

    dominant_narrative: e.g. "ISRAEL_WAR", "ELON_MARS", "TRUMP_TARIFFS"
    viral_events:       ranked list of viral stories with meme derivatives
    meme_keywords:      flat list of all meme-worthy keywords (coin name candidates)
    trending_tokens:    $TICKER symbols explicitly mentioned on Twitter
    trending_mints:     resolved Solana mints (if detected)
    raw_keywords:       all keywords sorted by engagement weight

    Note: og_signals is populated later by TokenDiscovery, not here.
    Twitter scanner only answers "what is viral?" — not "which tokens are pumping?".
    """
    dominant_narrative: str
    viral_events: list[ViralEvent]
    narratives: list[NarrativeScore]    # kept for compatibility with bot.py
    meme_keywords: list[str]
    trending_tokens: list[str]
    trending_mints: list[str]
    raw_keywords: list[str]
    # Populated by TokenDiscovery after scanning — not by twitter scanner
    og_signals: list = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def top_keywords(self, n: int = 20) -> list[str]:
        return self.meme_keywords[:n]

    def is_valid_hour_to_trade(self, optimal_hours: list, avoid_hours: list) -> bool:
        current_hour = datetime.now(timezone.utc).hour
        return current_hour not in avoid_hours and current_hour in optimal_hours

    def __repr__(self) -> str:
        events = " | ".join(e.topic for e in self.viral_events[:3])
        return f"NarrativeReport('{self.dominant_narrative}' | events=[{events}])"


# ── Main scanner ──────────────────────────────────────────────────────────────

class TwitterNarrativeScanner:
    """
    Viral event-driven narrative scanner.

    Instead of checking predefined categories, this scanner:
      - Fetches the most-engaged tweets from the last 2 hours
      - Clusters them by topic/entity
      - Extracts meme-worthy keywords that will appear as coin names on pump.fun
      - Separately checks OG Solana meme revival signals
    """

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.1d4.us",
    ]

    # Minimum engagement to consider a tweet "viral"
    VIRAL_IMPRESSION_THRESHOLD = 50_000
    VIRAL_ENGAGEMENT_THRESHOLD = 2_000   # likes + RTs combined

    def __init__(self):
        self._cache: Optional[NarrativeReport] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._use_api = bool(config.twitter_bearer_token)

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Accept": "application/json, text/html",
            },
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_narrative(self, force_refresh: bool = False) -> NarrativeReport:
        if self._cache and not force_refresh:
            age = datetime.now(timezone.utc) - self._cache.generated_at
            if age < timedelta(minutes=config.narrative_refresh_minutes):
                logger.debug("Returning cached narrative")
                return self._cache

        logger.info("Scanning Twitter for viral events...")
        report = await self._build_narrative()
        self._cache = report
        logger.info(repr(report))
        return report

    async def score_token_against_narrative(
        self, token_name: str, token_symbol: str, narrative: NarrativeReport
    ) -> float:
        """
        Score 0.0–1.0: how well this token matches today's viral events.

        Scoring logic:
          - Direct name/symbol match in meme_keywords:         +0.50
          - Fuzzy match (substring of keyword or vice versa):  +0.30
          - Token symbol in trending_tokens (Twitter $tickers):+0.40
          - OG meme revival signal active + token is OG:       +0.35
          - Generic viral bonus (any match in raw_keywords):   +0.15
        """
        score = 0.0
        name_l = token_name.lower().strip()
        sym_l = token_symbol.lower().strip()
        meme_kws = [k.lower() for k in narrative.meme_keywords]
        raw_kws = [k.lower() for k in narrative.raw_keywords]

        # 1. Exact match in meme keyword list
        for kw in meme_kws[:40]:
            if kw == name_l or kw == sym_l:
                score += 0.50
                break
            if kw in name_l or name_l in kw or kw in sym_l or sym_l in kw:
                score += 0.30
                break

        # 2. Explicit $TICKER mention on Twitter
        if sym_l.upper() in [t.upper() for t in narrative.trending_tokens]:
            score += 0.40

        # 3. Any match in raw keywords (weaker signal)
        if score < 0.15:
            for kw in raw_kws[:60]:
                if len(kw) >= 3 and (kw in name_l or kw in sym_l):
                    score += 0.15
                    break

        return min(score, 1.0)

    # ── Internal orchestration ────────────────────────────────────────────────

    async def _build_narrative(self) -> NarrativeReport:
        """Orchestrate all data sources and build the report.

        Twitter scanner is purely responsible for:
          - Fetching viral/high-engagement tweets
          - Clustering them into events ("story of the day")
          - Extracting meme-name derivatives per event

        It does NOT check token prices or on-chain data.
        OG narrative token detection is done later by TokenDiscovery.
        """
        trending_tokens: list[str] = []

        try:
            raw_viral = await self._fetch_viral_tweets()
        except Exception as e:
            logger.warning(f"Viral tweet fetch failed: {e}")
            raw_viral = []

        # Cluster raw viral tweets into events
        viral_events = self._cluster_into_events(raw_viral)

        # Extract all $TICKER mentions
        for tweet_text in raw_viral:
            trending_tokens.extend(self._extract_ticker_mentions(tweet_text))
        trending_tokens = list(dict.fromkeys(t.upper() for t in trending_tokens))

        # Build flat meme keyword list from all events
        meme_keywords: list[str] = []
        for event in viral_events:
            meme_keywords.extend(event.meme_derivatives)
            meme_keywords.extend(event.raw_keywords)

        # Add OG meme symbols if revival signals are strong
        for sig in og_signals:
            if sig.is_strong:
                meme_keywords.insert(0, sig.symbol.lower())

        # Deduplicate preserving order
        seen: set[str] = set()
        unique_kws: list[str] = []
        for kw in meme_keywords:
            kw_l = kw.lower().strip()
            if kw_l and kw_l not in seen:
                seen.add(kw_l)
                unique_kws.append(kw_l)

        # Determine dominant narrative label
        dominant = self._determine_dominant(viral_events, og_signals)

        # Build compatibility NarrativeScore list
        compat_scores = [
            NarrativeScore(
                category=e.topic.upper().replace(" ", "_")[:20],
                score=e.engagement_score,
                keywords_hit=e.meme_derivatives[:5],
                tweet_count=e.tweet_count,
                velocity=e.velocity,
                top_accounts=[],
            )
            for e in viral_events[:5]
        ]

        return NarrativeReport(
            dominant_narrative=dominant,
            viral_events=viral_events,
            narratives=compat_scores,
            meme_keywords=unique_kws,
            trending_tokens=trending_tokens,
            trending_mints=[],
            raw_keywords=unique_kws,
            # og_signals left empty here — populated by TokenDiscovery
        )

    # ── Viral tweet fetching ──────────────────────────────────────────────────

    async def _fetch_viral_tweets(self) -> list[str]:
        """
        Fetch high-engagement tweets from last 2h.
        Returns list of tweet text strings.
        """
        if self._use_api:
            try:
                return await self._fetch_viral_via_api()
            except Exception as e:
                logger.warning(f"Twitter API viral fetch failed: {e}")

        try:
            return await self._fetch_viral_via_nitter()
        except Exception as e:
            logger.warning(f"Nitter viral fetch failed: {e}")

        # Last resort: fetch from public trending sources
        return await self._fetch_from_trending_sources()

    async def _fetch_viral_via_api(self) -> list[str]:
        """Twitter API v2 — search top tweets by engagement in last 2h."""
        if not self._session:
            return []

        headers = {"Authorization": f"Bearer {config.twitter_bearer_token}"}
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        # Queries targeting crypto-adjacent viral content + world news
        queries = [
            # General high-engagement crypto/meme
            "(solana OR sol OR pumpfun OR memecoin) -is:retweet lang:en min_faves:500",
            # World news that spawns memes
            "(breaking OR viral OR trending OR news) -is:retweet lang:en min_faves:2000",
            # Alpha accounts' recent posts
            f"(from:{' OR from:'.join(VIRAL_SEED_ACCOUNTS[:8])}) -is:retweet",
        ]

        tweet_texts: list[str] = []

        for query in queries:
            params = {
                "query": query,
                "max_results": "100",
                "tweet.fields": "public_metrics,created_at,lang",
                "sort_order": "relevancy",
                "start_time": two_hours_ago,
            }
            try:
                async with self._session.get(
                    "https://api.twitter.com/2/tweets/search/recent",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status == 429:
                        logger.warning("Twitter API rate limited")
                        await asyncio.sleep(15)
                        continue
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for tweet in data.get("data", []):
                        metrics = tweet.get("public_metrics", {})
                        impressions = metrics.get("impression_count", 0)
                        likes = metrics.get("like_count", 0)
                        rts = metrics.get("retweet_count", 0)
                        engagement = likes + rts * 2

                        # Only keep genuinely viral content
                        if impressions >= self.VIRAL_IMPRESSION_THRESHOLD or engagement >= self.VIRAL_ENGAGEMENT_THRESHOLD:
                            tweet_texts.append(tweet.get("text", ""))

                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"API query error: {e}")

        logger.debug(f"Twitter API viral fetch: {len(tweet_texts)} viral tweets")
        return tweet_texts

    async def _fetch_viral_via_nitter(self) -> list[str]:
        """Scrape Nitter for high-engagement tweets from alpha accounts."""
        if not self._session:
            return []

        tweet_texts: list[str] = []

        for instance in self.NITTER_INSTANCES:
            try:
                # Scan viral seed accounts
                for account in VIRAL_SEED_ACCOUNTS[:10]:
                    try:
                        url = f"{instance}/{account}"
                        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                            if resp.status != 200:
                                continue
                            html = await resp.text()
                            tweets = self._extract_tweets_from_nitter_html(html)
                            tweet_texts.extend(tweets)
                        await asyncio.sleep(0.4)
                    except Exception:
                        continue

                # Scan for breaking news / viral content
                for tag in ["breaking", "viral", "solana", "meme"]:
                    try:
                        url = f"{instance}/search?q=%23{tag}+min_faves%3A500&f=tweets"
                        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                            if resp.status == 200:
                                html = await resp.text()
                                tweets = self._extract_tweets_from_nitter_html(html)
                                tweet_texts.extend(tweets)
                        await asyncio.sleep(0.3)
                    except Exception:
                        continue

                if tweet_texts:
                    break  # One working instance is enough

            except Exception as e:
                logger.debug(f"Nitter {instance} failed: {e}")

        logger.debug(f"Nitter viral fetch: {len(tweet_texts)} tweets")
        return tweet_texts

    async def _fetch_from_trending_sources(self) -> list[str]:
        """
        Fallback: fetch trending topics from public sources
        (trends24.in, getdaytrends.com, etc.)
        """
        if not self._session:
            return []

        tweet_texts: list[str] = []

        # trends24 is publicly accessible and shows trending Twitter topics
        sources = [
            ("https://trends24.in/united-states/", "trend-card__list"),
            ("https://trends24.in/worldwide/", "trend-card__list"),
        ]

        for url, css_hint in sources:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        # Extract trend names (they appear as list items)
                        trends = re.findall(r'<a[^>]+>([^<]{2,40})</a>', html)
                        for trend in trends:
                            trend = trend.strip()
                            if 2 < len(trend) < 40 and not trend.startswith("http"):
                                tweet_texts.append(trend)
            except Exception as e:
                logger.debug(f"Trending source {url} failed: {e}")

        logger.debug(f"Trending sources fallback: {len(tweet_texts)} items")
        return tweet_texts

    # ── Event clustering ──────────────────────────────────────────────────────

    def _cluster_into_events(self, tweet_texts: list[str]) -> list[ViralEvent]:
        """
        Cluster tweet texts into distinct viral events using keyword frequency.

        Algorithm:
          1. Extract all meaningful words/entities from all tweets
          2. Find top N "anchor words" (highest frequency, non-stopword)
          3. Each anchor word = one candidate event
          4. Assign tweets to nearest anchor
          5. Build meme derivatives for each event
        """
        if not tweet_texts:
            return [self._fallback_event()]

        # Count word frequencies across all tweets
        word_freq: Counter = Counter()
        all_entities: list[str] = []

        for text in tweet_texts:
            words = self._extract_meaningful_words(text)
            all_entities.extend(words)
            word_freq.update(words)

        # Top anchor words = the "stories of the day"
        # Filter out very generic words
        GENERIC_WORDS = {
            "crypto", "bitcoin", "ethereum", "blockchain", "token", "coin",
            "market", "price", "bull", "bear", "buy", "sell", "pump", "dump",
            "solana", "sol", "defi", "nft", "web3", "launch", "new",
        }
        anchor_candidates = [
            (word, count) for word, count in word_freq.most_common(100)
            if word not in GENERIC_WORDS and len(word) >= 3 and count >= 2
        ]

        if not anchor_candidates:
            return [self._fallback_event()]

        # Build events from top anchors (max 5 distinct events)
        events: list[ViralEvent] = []
        used_words: set[str] = set()

        for anchor_word, anchor_count in anchor_candidates[:20]:
            if anchor_word in used_words:
                continue
            if len(events) >= 5:
                break

            # Find tweets containing this anchor
            related_tweets = [
                t for t in tweet_texts
                if anchor_word in t.lower()
            ]
            if len(related_tweets) < 2:
                continue

            # Extract all words from related tweets
            related_words: Counter = Counter()
            for t in related_tweets:
                related_words.update(self._extract_meaningful_words(t))

            # Mark these words as "used" to avoid duplicate events
            top_related = [w for w, _ in related_words.most_common(10)]
            used_words.update(top_related[:5])

            # Build meme derivatives — the actual words that become coin names
            meme_derivs = self._generate_meme_derivatives(
                anchor_word, related_words
            )

            # Engagement score = log-scaled tweet count
            import math
            eng_score = min(math.log(len(related_tweets) + 1) / math.log(50), 1.0)

            events.append(ViralEvent(
                topic=self._humanize_topic(anchor_word, top_related),
                raw_keywords=top_related,
                meme_derivatives=meme_derivs,
                engagement_score=eng_score,
                tweet_count=len(related_tweets),
                velocity=len(related_tweets) / 2.0,   # per hour (2h window)
                sample_tweets=[t[:120] for t in related_tweets[:3]],
            ))

        if not events:
            events = [self._fallback_event()]

        events.sort(key=lambda e: e.engagement_score, reverse=True)
        logger.info(f"Detected {len(events)} viral events:")
        for e in events:
            logger.info(f"  {e}")

        return events

    # ── Meme derivative generation ────────────────────────────────────────────

    def _generate_meme_derivatives(
        self, anchor: str, related_words: Counter
    ) -> list[str]:
        """
        Generate the meme-coin name candidates from a viral event.

        Logic: When "Israel" trends → meme coins named ISRAEL, IDF, BIBI, NETANYAHU,
        GAZA, IDF, HAMAS (the people/places/things in the story).
        When "Elon Musk Mars" trends → ELON, MARS, SPACEX, ROCKET, X.
        These are the EXACT names that pump.fun creators use.
        """
        derivatives: list[str] = []
        derivatives.append(anchor.lower())

        # Add top related words as derivatives
        for word, _ in related_words.most_common(15):
            if len(word) >= 2 and word != anchor:
                derivatives.append(word.lower())

        # Add common meme suffix/prefix patterns
        meme_variations = []
        for deriv in derivatives[:5]:
            # Common pump.fun naming patterns
            meme_variations.append(deriv)
            meme_variations.append(f"{deriv}inu")      # e.g. "israelinu"
            meme_variations.append(f"${deriv.upper()}")

        derivatives.extend(meme_variations)

        # Deduplicate
        seen: set[str] = set()
        result: list[str] = []
        for d in derivatives:
            d_clean = d.lower().strip("$").strip()
            if d_clean and d_clean not in seen and len(d_clean) >= 2:
                seen.add(d_clean)
                result.append(d_clean)

        return result[:25]

    # ── Dominant narrative ────────────────────────────────────────────────────

    def _determine_dominant(
        self, events: list[ViralEvent], og_signals: list[OGMemeSignal]
    ) -> str:
        """Label the dominant narrative for logging / bot status display."""
        strong_og = [s for s in og_signals if s.is_strong]

        if strong_og and (not events or events[0].engagement_score < 0.5):
            # OG meme revival dominates when no viral news event
            symbols = "+".join(s.symbol for s in strong_og[:2])
            return f"OG_REVIVAL_{symbols}"

        if events:
            topic = events[0].topic.upper().replace(" ", "_").replace("/", "_")
            # Truncate for readability
            return topic[:30]

        return "UNKNOWN"

    # ── Fallback ──────────────────────────────────────────────────────────────

    def _fallback_event(self) -> ViralEvent:
        """Return a generic event when no data is available."""
        return ViralEvent(
            topic="Solana Meme Season",
            raw_keywords=["solana", "meme", "pump", "moon", "gem", "ape"],
            meme_derivatives=["sol", "meme", "doge", "pepe", "frog", "cat", "dog"],
            engagement_score=0.2,
            tweet_count=0,
            velocity=0.0,
            sample_tweets=[],
        )

    # ── Text helpers ──────────────────────────────────────────────────────────

    def _extract_meaningful_words(self, text: str) -> list[str]:
        """
        Extract meaningful words/entities from text.
        Keeps proper nouns, names, places, and meme-worthy terms.
        Drops common stop words and generic crypto jargon.
        """
        STOP_WORDS = {
            # English stop words
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "her", "was", "one", "our", "out", "day", "get", "has", "him",
            "his", "how", "its", "may", "new", "now", "old", "see", "two",
            "way", "who", "did", "got", "let", "put", "say", "she", "too",
            "use", "had", "man", "via", "this", "that", "with", "will",
            "from", "have", "been", "than", "then", "they", "them", "what",
            "when", "where", "which", "while", "about", "after", "before",
            "there", "their", "would", "could", "should", "more", "some",
            "just", "like", "also", "into", "over", "only", "very", "your",
            "time", "year", "make", "take", "come", "good", "most", "know",
            # Generic crypto
            "crypto", "token", "coin", "market", "price", "trading", "trade",
            "blockchain", "defi", "nft", "web3", "launch", "launched",
            "bullish", "bearish", "pump", "dump", "ath", "atl", "wallet",
            "buy", "sell", "hold", "hodl", "moon", "lfg", "based", "gm",
            "https", "http", "www", "com", "amp", "via",
        }

        # Remove URLs
        text = re.sub(r"https?://\S+", "", text)
        # Remove @mentions and #tags for word extraction (but keep the text)
        text = re.sub(r"[@#](\w+)", r" \1 ", text)
        # Normalize
        text = text.lower()
        # Extract words (including hyphenated)
        words = re.findall(r"[a-z][a-z\-']{1,30}", text)

        meaningful = [
            w for w in words
            if w not in STOP_WORDS
            and len(w) >= 2
            and not w.isdigit()
        ]
        return meaningful

    def _extract_ticker_mentions(self, text: str) -> list[str]:
        """Extract $TICKER mentions from tweet text."""
        # $TICKER pattern
        dollar_tickers = re.findall(r"\$([A-Za-z]{2,10})\b", text)
        # Cashtags in all-caps (common in crypto Twitter)
        caps_words = re.findall(r"\b([A-Z]{2,8})\b", text)

        SKIP = {
            "THE", "AND", "FOR", "BUT", "NOT", "YOU", "ALL", "CAN", "OUT",
            "GET", "NOW", "NEW", "SOL", "BTC", "ETH", "USD", "ATH", "ATL",
            "LFG", "GG", "IMO", "FUD", "FOMO", "DEX", "CEX", "APR", "APY",
            "NFT", "DAO", "AI", "USA", "WAS", "ARE", "HAS", "HAD", "DID",
            "ITS", "OUR", "WHO", "WHY", "HOW", "INC", "LLC", "CEO", "CFO",
        }
        combined = [t for t in dollar_tickers + caps_words if t.upper() not in SKIP]
        return list(dict.fromkeys(combined))[:20]

    @staticmethod
    def _extract_tweets_from_nitter_html(html: str) -> list[str]:
        """Extract tweet content from Nitter HTML response."""
        # Remove script/style
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
        # Extract tweet-content divs
        tweet_blocks = re.findall(
            r'class="tweet-content[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL
        )
        if not tweet_blocks:
            # Fallback: extract all text
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            return [s.strip() for s in text.split(".") if len(s.strip()) > 30][:30]

        results = []
        for block in tweet_blocks:
            clean = re.sub(r"<[^>]+>", " ", block)
            clean = re.sub(r"\s+", " ", clean).strip()
            if len(clean) > 20:
                results.append(clean)
        return results

    @staticmethod
    def _humanize_topic(anchor: str, related: list[str]) -> str:
        """Create a human-readable topic label."""
        # Use anchor + top 2 related words
        parts = [anchor] + [w for w in related[:2] if w != anchor]
        return " ".join(parts[:3]).title()
