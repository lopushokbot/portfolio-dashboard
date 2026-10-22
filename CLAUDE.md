# Stablecoin APY Dashboard

## Overview
Auto-updating DeFi stablecoin yield comparison dashboard. Fetches APY data from 10 protocols (Aave shown as V3 + V4) via direct APIs, plus a My Positions card for the tracked wallets, generates a static HTML page, deployed to GitHub Pages via daily GitHub Actions cron.

## Live URLs
| Environment | URL |
|-------------|-----|
| Live | https://lopushokbot.github.io/portfolio-dashboard/apy_dashboard.html |
| GitHub | https://github.com/lopushokbot/portfolio-dashboard |
| Local path | `/Users/iibot/Documents/ppppp/workspace/apy-dashboard/` |

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
| Maple Finance | `api.maple.finance/v2/graphql` | POST (GraphQL) | `spotApy / 1e28` = percentage. Hidden by request (`MAPLE_EXCLUDE`): Institutional – Secured Lending, Receivables Financing |
| Ethena (sUSDe) | `app.ethena.fi/api/yields/protocol-and-staking-yield` | GET | `stakingYield.value` = APY % |
| Sky (sUSDS) | `info-sky.blockanalitica.com/api/v1/overall/` | GET | `sky_savings_rate_apy * 100` |
| Falcon Finance | `api.falcon.finance/api/v1/statistics` | GET | `sUSDf_7d_apy * 100` |
| Hyperliquid HLP | `api.hyperliquid.xyz/info` | POST | `vaultDetails` for HLP vault address |
| Hyperliquid Earn | `api.hyperliquid.xyz/info` | POST | `allBorrowLendReserveStates` (native HyperCore lending) + `spotMeta` for token names (0 = USDC, 268 = USDT0, 360 = USDH). `supplyYearlyRate` decimal (× 100). Card shows stablecoin reserves ≥ $100K supplied, sorted by APY. Supply APY = 5% flat borrow rate (below 80% util) × utilization × 0.9 |
| Aave V3 | `api.v3.aave.com/graphql` | POST (GraphQL) | All chains; pools ≥ $50M supplied only (`AAVE_MIN_TVL`), USDC/USDT also need APY ≥ 4%. Exception: EURC on Ethereum + Base always shown (min $100K). `supplyInfo.apy.value` is decimal (× 100 for %) |
| Aave V4 | `api.v4.aave.com/graphql` (official; DefiLlama `aave-v4` as fallback) | POST (GraphQL) | `reserves(request:{query:{chainIds}})` across all mainnet V4 chains (`chains(MAINNET_ONLY)`). APY = `supplyApy` + Merkl `extraApy`. Card rule: USDC/USDT with ≥ $10M supplied AND APY > 4%; EURC always (≥ $25K); pools where My Positions has a supply always shown with a "Held" tag (matched on chain + official spoke name + symbol) |

All APIs are public, no auth needed.

## My Positions card (tracked wallets)
Top card: APYs of the tracked wallets' DeFi positions in one table — **position name, asset, APY only** + two blended-APY tiles (stablecoin / overall). Rows are **grouped by protocol** (Aave V3 + V4 = one "Aave" group with a V3/V4 pill per row); groups ordered by blended APY, rows by APY; multi-position groups show their blended APY in the header.
- **Privacy rules (owner's explicit request — never relax):**
  - No balances anywhere: not on the page, not in Actions logs (public repo). USD sizes are used only to weight blended APYs, the <$50 dust filter and loan net-APY. Loan detail lines show rates only.
  - No wallet identity anywhere: addresses live only in the `APY_WALLETS` Actions secret (comma-separated) / gitignored `wallets.local.json` (`["0x…","0x…"]`). The page never mentions wallets; `load_wallets()` emits `::add-mask::` for every address form so CI logs redact them. Don't name the owner in public files or commit messages.
  - No wallets configured → card is skipped.
- Positions are **discovered live** each run (new deposits appear, exits drop off; < $50 hidden). Dedicated fetchers:

| Source | How | APY |
|--------|-----|-----|
| Aave V3 | `userSupplies`/`userBorrows` on `api.v3.aave.com` across all markets | API `apy.value` |
| Aave V4 | `userSupplies`/`userBorrows` (`userChains`) on `api.v4.aave.com` | `supplyApy` + Merkl `extraApy`; borrow = `borrowApy` − Merkl `discountApy` |
| Morpho | `userByAddress` (vaultV2Positions + vaultPositions), chains 1/8453/42161 | vault `netApy` (incl. rewards) |
| Maple | `account(id).poolV2Positions` | pool `spotApy / 1e28` |
| Fluid | `api.fluid.instadapp.io/v2/lending/{chain}/users/{addr}/positions` | DefiLlama (incl. Merkl), fallback Fluid `totalRate` |
| sUSDf, sUSDe | on-chain ERC-4626 `balanceOf` → `convertToAssets` (publicnode RPC) | Falcon / Ethena APIs (same as their cards) |
| Uniswap V3 LPs | on-chain NFT enumeration + `positions` + pool `slot0` on Eth/Arb/Base | DefiLlama pool 7d fee APR if in range, 0 if out of range |
| Hyperliquid | `delegatorSummary`/`delegations` (HYPE staking), `userVaultEquities` (vaults), `borrowLendUserState` (Earn supply/borrow → "Earn" row; net row if borrowing) | validator `predictedApr` (month); HLP from vault APR; Earn from reserve `supplyYearlyRate`/`borrowYearlyRate` |

- **Auto-discovery of new protocols** (`discover_vaults()`, second pass): any **ERC-4626 vault token** a wallet holds that no fetcher above already reported (fetchers tag rows with their receipt-token address).
  - Candidates — Ethereum: full token list from Routescan (`api.routescan.io/v2/network/mainnet/evm/1/address/{a}/erc20-holdings`, Blockscout fallback) + log scan. Arbitrum/Base: no free token-list API (Blockscout is Cloudflare-blocked), so each run scans incoming `Transfer` logs since the last scanned block and remembers tokens in `.positions-state.json` (CI: Actions cache `positions-state-*`; local: gitignored). → catches every position opened since 2026-10-20 on those chains.
  - Log-scan RPCs: Tenderly public gateways (Eth/Arb allow 1M-block ranges; Base only 1,000 → ~43 calls/day). publicnode/drpc/blast/nodies don't allow topic log queries on free tiers.
  - Spam guard: must implement `asset()`, underlying priced by coins.llama.fi, vault `totalAssets` ≥ $1M.
  - APY: DefiLlama pool with same chain + symbol (= vault symbol) + underlying → that APY and project name (`_project_name`). Else realized 7-day share-price growth via archive RPCs (Eth: drpc/mevblocker/tenderly; Arb: 1rpc; Base: mainnet.base.org) and group "Other vaults".
  - Not auto-discoverable (need a dedicated fetcher): rebasing aTokens of Aave forks, non-token positions (spoke/NFT/staking like Aave V4, Uniswap, Hyperliquid), Pendle PTs, non-EVM chains.
- Lending markets with debt (e.g. Aave V4 Base "Coinbase Stocks": GOOGLc collateral / USDC debt) are collapsed into one row showing net APY on equity (supply/borrow rates in the detail line).
- Not covered (no yield): Hyperliquid spot balances & perps, plain wallet tokens, Merkl unclaimed rewards.

## Deployment
- **Automated**: GitHub Actions cron runs daily at 5:00 UTC (9:00 AM Dubai)
- **Manual**: Run `python scripts/generate_dashboard.py` locally, then commit and push `apy_dashboard.html`

## Known Issues & Gotchas
- **Ethena API**: Sometimes returns empty responses from GitHub Actions IPs (Cloudflare blocking) — handled by retry logic (3 retries, exponential backoff)
- **Morpho API**: Occasional 504 Gateway Timeout — handled by retry logic
- **Maple spotApy**: Raw value is scaled by 1e28 — must divide to get percentage
- **DefiLlama**: Returns ALL pools, filtered client-side by project name and chain — response is large (~2MB)
- **Aave V3 API**: GraphQL endpoint at `api.v3.aave.com/graphql`. Each chain may have multiple markets (e.g., Ethereum has Main, Lido, EtherFi, Horizon — they appear as separate `markets[]` entries with `name` like `AaveV3EthereumLido`). The dashboard strips the `AaveV3<Chain>` prefix to label sub-markets. `supplyInfo.apy.value` is a decimal fraction — multiply by 100 for percentage.
- **Aave V4 card source**: official API since 2026-10-20 (was DefiLlama). Official spoke names (Main, Forex, Bluechip, Maple SyrupUSDG, Coinbase Stocks…) differ from DefiLlama's (Core/Prime/Plus/Global Dollar) — that's why the card switched: "Held" matching needs the names My Positions uses. Request objects need a `query` sub-object; introspection is disabled — copy query shapes from the `@aave/graphql` npm package (`dist/index.js`). DefiLlama fallback can't mark Held rows. Main-spoke USDC/USDG rates include large Merkl campaigns that end/renew (dates in `rewards{ endDate }`).
- **Responsive layout**: `.card{min-width:0}` is required or wide tables stretch grid cells past the phone viewport. Phone breakpoint `max-width:600px` (tighter padding, source labels hidden, first column wraps); 2-column grid from 640px (iPad).
- **Morpho API schema drift**: the vault filter field was renamed `whitelisted` → `listed` (returns HTTP 400 `Field "whitelisted" is not defined` if you use the old name). Fixed 2026-06-05.
