# Stablecoin APY Dashboard

## Overview
Auto-updating DeFi stablecoin yield comparison dashboard. Fetches APY data from 8 protocols via direct APIs, generates a static HTML page, deployed to GitHub Pages via daily GitHub Actions cron.

## Live URLs
| Environment | URL |
|-------------|-----|
| Live | https://lopushokbot.github.io/portfolio-dashboard/apy_dashboard.html |
| GitHub | https://github.com/lopushokbot/portfolio-dashboard |
| Local path | `/Users/iibot/Documents/ppppp/workspace/portfolio-dashboard/` |

## Tech Stack
- Python 3 (stdlib only — `urllib`, `json`, `concurrent.futures`)
- No external dependencies — runs anywhere without pip install
- GitHub Actions for CI/CD
- Static HTML output (single file, self-contained)

## Architecture
```
workspace/portfolio-dashboard/
├── scripts/
│   └── generate_dashboard.py    # Main script — fetches all APIs, generates HTML
├── .github/
│   └── workflows/
│       └── update-dashboard.yml # Daily cron at 5:00 UTC (9 AM Dubai)
└── apy_dashboard.html           # Generated output (committed to repo)
```

Single-file architecture: `generate_dashboard.py` does everything — fetch, parse, validate, render HTML.

## Data Sources
| Protocol | API Endpoint | Method | Notes |
|----------|-------------|--------|-------|
| Fluid Lending | `yields.llama.fi/pools` (DefiLlama) | GET | Fluid's own API misses Merkle rewards on ETH/ARB/Base |
| Jupiter Lend | `yields.llama.fi/pools` (DefiLlama) | GET | Stables: USDC, JUPUSD, USDT, USDS, EURC |
| Morpho | `blue-api.morpho.org/graphql` | POST (GraphQL) | Vaults with TVL >= $15M, net APY >= 3.5% |
| Maple Finance | `api.maple.finance/v2/graphql` | POST (GraphQL) | `spotApy / 1e28` = percentage |
| Ethena (sUSDe) | `app.ethena.fi/api/yields/protocol-and-staking-yield` | GET | `stakingYield.value` = APY % |
| Sky (sUSDS) | `info-sky.blockanalitica.com/api/v1/overall/` | GET | `sky_savings_rate_apy * 100` |
| Falcon Finance | `api.falcon.finance/api/v1/statistics` | GET | `sUSDf_7d_apy * 100` |
| Hyperliquid HLP | `api.hyperliquid.xyz/info` | POST | `vaultDetails` for HLP vault address |

All APIs are public, no auth needed.

## Deployment
- **Automated**: GitHub Actions cron runs daily at 5:00 UTC (9:00 AM Dubai)
- **Manual**: Run `python scripts/generate_dashboard.py` locally, then commit and push `apy_dashboard.html`

## Known Issues & Gotchas
- **Ethena API**: Sometimes returns empty responses from GitHub Actions IPs (Cloudflare blocking) — handled by retry logic (3 retries, exponential backoff)
- **Morpho API**: Occasional 504 Gateway Timeout — handled by retry logic
- **Maple spotApy**: Raw value is scaled by 1e28 — must divide to get percentage
- **DefiLlama**: Returns ALL pools, filtered client-side by project name and chain — response is large (~2MB)
