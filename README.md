# Solana Narrative Trading Bot

Autonomous trading bot for Solana, modelled on wallet **AF6syABApBp7d1NfjUkmKG7BBBEWVMvXadyvqQgjLnRN** (Feb 2026: +$17,230 PnL, 71% WR, 323 trades).

---

## Strategy (derived from wallet analysis)

| Rule | Value | Source |
|------|-------|--------|
| Entry cap (golden zone) | $5k – $20k MC | 79% WR, best PnL in analysis |
| Avoid zone | $20k – $100k MC | 58% WR, net negative |
| Extended zone | $100k – $500k MC | Only with strong narrative |
| Position size | $100 – $300 per trade | 82% WR (best WR of any size bucket) |
| Max buys per token | **1** (no DCA) | 1 buy = 79% WR, 2+ = 45% WR |
| Target hold | 30 min – 2h | "Conviction window" — most profitable |
| Hard exit | 6 hours | >6h = disaster zone in analysis |
| Stop loss | 25% | Configurable |
| Take profit | 150% (2.5x) | Configurable |
| Routing | Jupiter → pump.fun | 74% of winning trades used this path |
| Trade hours | UTC 4h, 13–15h | Best performance hours |
| Avoid hours | UTC 6h, 8h, 12h, 22h | Worst performance hours |

---

## Architecture

```
bot.py                  ← Main orchestrator / event loop
├── twitter_scanner.py  ← Daily narrative detection from Twitter/X
├── token_discovery.py  ← pump.fun + Raydium + DexScreener scanner
├── jupiter_client.py   ← Jupiter v6 swap execution
├── risk_manager.py     ← Position sizing, stop-loss, time limits
└── config.py           ← All parameters (env-driven)
```

### Narrative → Trade flow

```
Twitter scan (every 60 min)
    ↓
Dominant narrative detected (AI_AGENTS / DEPIN / MEME_META / ...)
    ↓
Token discovery (every 2 min)
  - pump.fun latest & trending
  - DexScreener narrative search
  - Birdeye trending (optional)
    ↓
Hard filter: MC $5k-$20k, liq ≥ $3k, age 2-1440 min
    ↓
Score: narrative alignment (40%) + MC position (25%) + liquidity (15%) + age (10%) + pump routing (10%)
    ↓
Top candidate → Risk check → Buy via Jupiter
    ↓
Monitor every 30s → exit at SL / TP / 6h / conviction window
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your keys
```

Required:
- `PRIVATE_KEY` — base58 Solana wallet private key
- `RPC_URL` — Solana RPC endpoint (Helius recommended)

Optional but improves performance:
- `TWITTER_BEARER_TOKEN` — Twitter API v2 for live narrative scanning
- `HELIUS_API_KEY` — better on-chain data
- `BIRDEYE_API_KEY` — trending token data

### 3. Run in dry-run mode first

```bash
DRY_RUN=true python bot.py
```

### 4. Switch to live trading

```bash
# In .env:
DRY_RUN=false
python bot.py
```

---

## Risk warning

This bot trades real money when `DRY_RUN=false`. Always:
- Test on dry run for several days first
- Start with small `TRADE_AMOUNT_SOL` (0.1–0.2 SOL)
- Set `DAILY_LOSS_LIMIT_SOL` conservatively
- Monitor logs closely

---

## Key config parameters

| Env var | Default | Description |
|---------|---------|-------------|
| `TRADE_AMOUNT_SOL` | 0.5 | SOL per trade |
| `MIN_MARKET_CAP` | 5000 | Minimum MC in USD |
| `MAX_MARKET_CAP` | 20000 | Max MC in USD (golden zone cap) |
| `STOP_LOSS_PCT` | 25 | Stop loss % |
| `TAKE_PROFIT_PCT` | 150 | Take profit % |
| `MAX_HOLD_HOURS` | 6 | Hard time exit |
| `MAX_DAILY_TRADES` | 20 | Trades per day cap |
| `MAX_CONCURRENT_POSITIONS` | 3 | Open positions cap |
| `DRY_RUN` | true | Simulate without sending txs |
| `NARRATIVE_REFRESH_MINUTES` | 60 | Twitter rescan interval |
