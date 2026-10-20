#!/usr/bin/env python3
"""Fetch DeFi yield data from multiple APIs and generate a static HTML dashboard.

Data sources:
  - Fluid Lending:    DefiLlama yields API (captures Merkle reward APY that Fluid API misses)
  - Jupiter Lend:     DefiLlama yields API
  - Aave V3:          Aave GraphQL API (api.v3.aave.com, USDC/USDT/EURC across all chains, APY >= 4%)
  - Morpho:           Morpho Blue GraphQL API (blue-api.morpho.org)
  - Maple Finance:    Maple GraphQL API (api.maple.finance)
  - Ethena (sUSDe):   Ethena REST API (app.ethena.fi)
  - Sky (sUSDS):      BlockAnalitica REST API (info-sky.blockanalitica.com)
  - Falcon Finance:   Falcon REST API (api.falcon.finance)
  - Hyperliquid HLP:  Hyperliquid REST API (api.hyperliquid.xyz)
  - My Positions:     APYs of the tracked wallets' DeFi positions (addresses from the APY_WALLETS
                      env var / wallets.local.json — never committed, never shown). Aave V3/V4, Morpho,
                      Maple, Fluid, Hyperliquid via APIs; sUSDf, sUSDe, Uniswap V3 LPs on-chain; any
                      other ERC-4626 vault auto-discovered.
"""

import json
import math
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────────────────────────────────

DEFILLAMA_URL = "https://yields.llama.fi/pools"
MORPHO_GQL_URL = "https://blue-api.morpho.org/graphql"
MAPLE_GQL_URL = "https://api.maple.finance/v2/graphql"
ETHENA_YIELD_URL = "https://app.ethena.fi/api/yields/protocol-and-staking-yield"
ETHENA_TVL_URL = "https://app.ethena.fi/api/collateralization/status"
SKY_URL = "https://info-sky.blockanalitica.com/api/v1/overall/"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
FALCON_URL = "https://api.falcon.finance/api/v1/statistics"
AAVE_GQL_URL = "https://api.v3.aave.com/graphql"

# Aave V3 supported mainnet chain IDs (from chains(filter: MAINNET_ONLY))
AAVE_CHAIN_IDS = [1, 42161, 8453, 43114, 137, 10, 59144, 5000, 56, 100, 324, 534352, 1088, 42220, 1868, 146, 9745, 57073, 196]

AAVE_QUERY = """query AaveMarkets($chainIds: [ChainId!]!) {
  markets(request: { chainIds: $chainIds }) {
    name
    address
    chain { name chainId }
    reserves {
      underlyingToken { symbol }
      size { usd }
      supplyInfo { apy { value } }
      isFrozen
      isPaused
    }
  }
}"""

FLUID_POOLS = [
    ("fluid-lending", "Ethereum", ["USDC", "USDT"]),
    ("fluid-lending", "Arbitrum", ["USDC", "USD₮0"]),
    ("fluid-lending", "Base", ["USDC", "EURC"]),
    ("venus-flux", "BSC", ["USDC", "USDT"]),
]

JUPITER_STABLES = ["USDC", "JUPUSD", "USDT", "USDS", "EURC"]

AAVE_STABLES = ("USDC", "USDT", "EURC")
AAVE_MIN_APY_USD = 4.0  # USDC/USDT threshold; EURC always shown (rarer asset, any rate is informative)
AAVE_MIN_TVL = 50_000_000  # only deep pools — small ones have volatile, utilization-driven rates
# EURC on these chains is always shown regardless of pool size; min stays at $100K
AAVE_EURC_KEEP_CHAINS = ("Ethereum", "Base")
AAVE_EURC_KEEP_MIN_TVL = 100_000

# Aave V4 (hub-and-spoke, launched on Ethereum mainnet 2026-03-30). Sourced from DefiLlama's
# aave-v4 project — same /pools response we already fetch, no extra API call. Each spoke (core /
# prime / plus) is a separate user-facing market with its own risk params and rate.
AAVE_V4_PROJECT = "aave-v4"
AAVE_V4_SYMBOL_MAP = {"USDC": "USDC", "USDT": "USDT", "EURC": "EURC", "EUROC": "EURC"}  # DefiLlama labels EURC as EUROC
AAVE_V4_MIN_TVL = 25_000  # dust floor for always-shown rows (EURC, held positions)
# Card rule: pools with >= $10M supplied AND APY > 4% (incl. Merkl rewards). Always shown regardless:
# EURC, and any pool where a tracked wallet has a supply position (from the My Positions fetch).
AAVE_V4_STABLES = {"USDC": "USDC", "USDT": "USDT", "USDt": "USDT", "EURC": "EURC"}
AAVE_V4_CARD_MIN_TVL = 10_000_000
AAVE_V4_CARD_MIN_APY = 4.0
AAVE_V4_RESERVES_QUERY = """query($c:[ChainId!]!){ reserves(request:{query:{chainIds:$c}}) {
  canSupply spoke{ name chain{ name } } asset{ underlying{ info{ symbol } } }
  summary{ supplyApy{ normalized } supplied{ exchange(currency:USD){ value } }
    rewards{ __typename ... on MerklSupplyReward { extraApy{ normalized } } } } } }"""

MORPHO_QUERY = """{
  vaults(
    first: 50
    orderBy: TotalAssetsUsd
    orderDirection: Desc
    where: { listed: true, totalAssetsUsd_gte: 15000000 }
  ) {
    items {
      name
      chain { network }
      asset { symbol }
      state { netApy totalAssetsUsd }
    }
  }
}"""

# Pools hidden from the Maple card (matched as substrings of the pool name)
MAPLE_EXCLUDE = ("Institutional - Secured Lending", "Receivables Financing")

MAPLE_QUERY = """{
  poolV2S {
    name
    asset { symbol decimals price }
    poolMeta { poolName state hidden }
    spotApy
    apyData { apy }
    tvlUsd
  }
}"""


# ── Data fetching ──────────────────────────────────────────────────────────────────────

def fetch_json(url, method="GET", data=None, headers=None, retries=3):
    hdrs = {"Content-Type": "application/json", "User-Agent": "APY-Dashboard/1.0"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    raise ValueError("Empty response body")
                return json.loads(raw)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                import time
                wait = 2 ** attempt
                print(f"    Retry {attempt+1}/{retries-1} for {url.split('/')[2]} (waiting {wait}s): {e}")
                time.sleep(wait)
    raise last_err


def fetch_defillama():
    print("  Fetching DefiLlama yields...")
    return fetch_json(DEFILLAMA_URL)["data"]


def fetch_morpho():
    print("  Fetching Morpho vaults...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Origin": "https://app.morpho.org",
        "Referer": "https://app.morpho.org/",
    }
    return fetch_json(MORPHO_GQL_URL, method="POST", data={"query": MORPHO_QUERY}, headers=headers)["data"]["vaults"]["items"]


def fetch_maple():
    print("  Fetching Maple Finance...")
    return fetch_json(MAPLE_GQL_URL, method="POST", data={"query": MAPLE_QUERY})["data"]["poolV2S"]


def fetch_ethena():
    print("  Fetching Ethena...")
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    )
    extra = {"User-Agent": browser_ua}
    yields = fetch_json(ETHENA_YIELD_URL, headers=extra)
    tvl_data = fetch_json(ETHENA_TVL_URL, headers=extra)
    return {"yields": yields, "tvl_data": tvl_data}


def fetch_sky():
    print("  Fetching Sky...")
    return fetch_json(SKY_URL)


def fetch_hyperliquid():
    print("  Fetching Hyperliquid HLP...")
    return fetch_json(
        HYPERLIQUID_URL, method="POST",
        data={"type": "vaultDetails", "vaultAddress": "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"},
    )


def fetch_falcon():
    print("  Fetching Falcon Finance...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://app.falcon.finance/",
        "Origin": "https://app.falcon.finance",
    }
    return fetch_json(FALCON_URL, headers=headers)


def fetch_aave_v4():
    print("  Fetching Aave V4 reserves...")
    return gql(AAVE_V4_GQL_URL, AAVE_V4_RESERVES_QUERY, {"c": _aave_v4_chain_ids()})["reserves"]


def fetch_aave():
    print("  Fetching Aave V3 markets...")
    return fetch_json(
        AAVE_GQL_URL, method="POST",
        data={"query": AAVE_QUERY, "variables": {"chainIds": AAVE_CHAIN_IDS}},
    )["data"]["markets"]


# ── Data processing ─────────────────────────────────────────────────────────────────────

def fmt_apy(apy):
    if apy is None: return "—"
    return f"{apy:.2f}%"

def apy_class(apy):
    if apy is None: return ""
    if apy < 0: return "apy-neg"
    if apy >= 5: return "apy-mid"
    return ""

def fmt_tvl(usd):
    if usd is None: return "—"
    if usd >= 1e9: return f"${usd / 1e9:.2f}B"
    if usd >= 1e6: return f"${round(usd / 1e6)}M"
    if usd >= 1e3: return f"${round(usd / 1e3)}K"
    return f"${round(usd)}"


def process_fluid(pools):
    rows = []
    for project, chain, symbols in FLUID_POOLS:
        for sym in symbols:
            matches = sorted(
                [p for p in pools if p.get("project") == project and p.get("chain") == chain
                 and p.get("symbol") == sym and (p.get("tvlUsd") or 0) > 100_000],
                key=lambda p: p.get("tvlUsd") or 0, reverse=True,
            )
            if matches:
                p = matches[0]
                rows.append({"chain": chain, "asset": "USDT" if sym == "USD₮0" else sym,
                             "apy": p.get("apy") or 0, "tvl": p.get("tvlUsd") or 0})
    return rows


def process_morpho(vaults):
    results = []
    for v in vaults:
        asset = v["asset"]["symbol"]
        if asset not in ("USDC", "USDT"): continue
        net_apy = (v["state"]["netApy"] or 0) * 100
        tvl = v["state"]["totalAssetsUsd"] or 0
        if net_apy < 3.5 or net_apy > 200 or tvl < 15_000_000: continue
        chain = v["chain"]["network"]
        results.append({"name": v["name"], "chain": chain[0].upper() + chain[1:] if chain else "",
                        "asset": asset, "apy": net_apy, "tvl": tvl})
    return sorted(results, key=lambda x: x["tvl"], reverse=True)


def process_maple(raw_pools):
    results = []
    for p in raw_pools:
        sym = p.get("asset", {}).get("symbol", "")
        if sym not in ("USDC", "USDT"): continue
        meta = p.get("poolMeta") or {}
        if meta.get("hidden"): continue
        tvl_usd = float(p.get("tvlUsd") or "0")
        if tvl_usd < 1_000_000: continue
        spot_apy = float(p.get("spotApy") or 0) / 1e28
        name = meta.get("poolName") or p.get("name", "")
        if any(x.lower() in name.lower() for x in MAPLE_EXCLUDE): continue
        results.append({"name": name, "asset": sym, "apy": spot_apy, "tvl": tvl_usd})
    return sorted(results, key=lambda x: x["tvl"], reverse=True)


def _aave_market_label(market_name, chain_name):
    """Strip 'AaveV3<Chain>' prefix → '' for main market, or 'Lido'/'EtherFi' etc. for sub-markets."""
    prefix = f"AaveV3{chain_name.replace(' ', '')}"
    if market_name.startswith(prefix):
        return market_name[len(prefix):]
    return market_name


def process_aave(markets):
    rows = []
    for m in markets or []:
        chain = (m.get("chain") or {}).get("name") or ""
        sub = _aave_market_label(m.get("name") or "", chain)
        for r in m.get("reserves") or []:
            if r.get("isFrozen") or r.get("isPaused"): continue
            sym = (r.get("underlyingToken") or {}).get("symbol")
            if sym not in AAVE_STABLES: continue
            try:
                apy = float(r["supplyInfo"]["apy"]["value"]) * 100
                tvl = float(r["size"]["usd"])
            except (KeyError, TypeError, ValueError):
                continue
            if sym == "EURC" and chain in AAVE_EURC_KEEP_CHAINS:
                if tvl < AAVE_EURC_KEEP_MIN_TVL: continue
            elif tvl < AAVE_MIN_TVL: continue
            if sym in ("USDC", "USDT") and apy < AAVE_MIN_APY_USD: continue
            rows.append({"chain": chain, "market": sub, "asset": sym, "apy": apy, "tvl": tvl})
    return sorted(rows, key=lambda r: r["apy"], reverse=True)


def _aave_v4_keep(sym, apy, tvl, held):
    if held: return tvl >= 1
    if sym == "EURC": return tvl >= AAVE_V4_MIN_TVL
    return tvl >= AAVE_V4_CARD_MIN_TVL and apy > AAVE_V4_CARD_MIN_APY


def process_aave_v4_official(reserves, held_keys):
    """Official Aave V4 API reserves → card rows. held_keys = {(chain, spoke, symbol)} of tracked supplies."""
    rows = []
    for r in reserves or []:
        if not r.get("canSupply"): continue
        raw_sym = r["asset"]["underlying"]["info"]["symbol"]
        chain, spoke = r["spoke"]["chain"]["name"], r["spoke"]["name"]
        held = (chain, spoke, raw_sym) in held_keys
        sym = AAVE_V4_STABLES.get(raw_sym, raw_sym if held else None)
        if sym is None: continue
        summ = r["summary"]
        try:
            apy = float(summ["supplyApy"]["normalized"]) + sum(
                float(x["extraApy"]["normalized"]) for x in summ.get("rewards") or [] if x.get("extraApy"))
            tvl = float(summ["supplied"]["exchange"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _aave_v4_keep(sym, apy, tvl, held): continue
        rows.append({"chain": chain, "spoke": spoke, "asset": sym, "apy": apy, "tvl": tvl, "held": held})
    return sorted(rows, key=lambda r: r["apy"], reverse=True)


def process_aave_v4(pools):
    """Fallback when the official V4 API is down: DefiLlama aave-v4 pools, same thresholds (no held matching —
    DefiLlama's spoke names differ from the official ones)."""
    rows = []
    for p in pools:
        if p.get("project") != AAVE_V4_PROJECT: continue
        sym = AAVE_V4_SYMBOL_MAP.get(p.get("symbol"))
        if sym is None: continue
        tvl = p.get("tvlUsd") or 0
        if tvl < AAVE_V4_MIN_TVL: continue
        spoke_raw = (p.get("poolMeta") or "").strip()
        spoke = spoke_raw[:1].upper() + spoke_raw[1:] if spoke_raw else ""
        chain = p.get("chain") or ""
        apy = p.get("apy") or 0
        if not _aave_v4_keep(sym, apy, tvl, False): continue
        rows.append({"chain": chain, "spoke": spoke, "asset": sym, "apy": apy, "tvl": tvl, "held": False})
    return sorted(rows, key=lambda r: r["apy"], reverse=True)


def process_jupiter(pools):
    # Match "jupiter-lend" exactly, or any "jupiter*" slug on Solana as a rebrand guard.
    jup_pools = [
        p for p in pools
        if (p.get("project") == "jupiter-lend"
            or "jupiter" in (p.get("project") or "").lower())
        and p.get("chain") == "Solana"
        and p.get("symbol") in JUPITER_STABLES
    ]
    if jup_pools:
        slugs = {p["project"] for p in jup_pools}
        if slugs != {"jupiter-lend"}:
            print(f"    [Jupiter] project slug(s) in use: {slugs}")
    if not jup_pools:
        # Diagnostic: log top Solana pools so we can spot a slug rename
        solana_projects = sorted({p["project"] for p in pools if p.get("chain") == "Solana"
                                   and (p.get("tvlUsd") or 0) > 5_000_000})
        print(f"    [Jupiter] 0 pools found. Top Solana projects in DefiLlama: {solana_projects[:20]}")
    jup = sorted(jup_pools, key=lambda p: p.get("tvlUsd") or 0, reverse=True)
    seen, deduped = set(), []
    for p in jup:
        if p["symbol"] not in seen:
            seen.add(p["symbol"])
            deduped.append(p)
    order = {s: i for i, s in enumerate(JUPITER_STABLES)}
    deduped.sort(key=lambda p: order.get(p["symbol"], 99))
    return [{"asset": p["symbol"], "apy": p.get("apy") or 0, "tvl": p.get("tvlUsd") or 0} for p in deduped]


def process_ethena(data):
    yields = data.get("yields") or {}
    tvl_data = data.get("tvl_data") or {}
    apy = yields.get("stakingYield", {}).get("value")
    tvl = tvl_data.get("totalBackingAssetsInUsd")
    if apy is not None and tvl is not None:
        return {"apy": float(apy), "tvl": float(tvl)}
    return None


def process_sky(data):
    if not data or not isinstance(data, list) or len(data) == 0: return None
    main = data[0]
    apy_raw, tvl_raw = main.get("sky_savings_rate_apy"), main.get("sky_savings_rate_tvl")
    if apy_raw is not None and tvl_raw is not None:
        return {"apy": float(apy_raw) * 100, "tvl": float(tvl_raw)}
    return None


def process_hlp(data):
    apr_raw = data.get("apr")
    apr_pct = (apr_raw * 100) if apr_raw is not None else None
    tvl = None
    for period in (data.get("portfolio") or []):
        if isinstance(period, list) and len(period) >= 2:
            hist = period[1].get("accountValueHistory", []) if isinstance(period[1], dict) else []
            if hist:
                tvl = float(hist[-1][1])
                break
    return {"apr": apr_pct, "tvl": tvl}


def process_falcon(data):
    apy_raw = data.get("sUSDf_7d_apy") or data.get("7d_apy")
    apy = float(apy_raw) * 100 if apy_raw else None
    tvl_raw = data.get("tvl")
    tvl = float(str(tvl_raw).replace(",", "")) if tvl_raw else None
    staked_raw = data.get("usdf_staked")
    staked = float(str(staked_raw).replace(",", "")) if staked_raw else None
    return {"apy": apy, "tvl": tvl, "staked": staked}


def process_ethena_defillama(pools):
    """Fallback: pull sUSDe APY from DefiLlama when Ethena API is Cloudflare-blocked."""
    matches = sorted(
        [p for p in pools if (
            "ethena" in (p.get("project") or "").lower()
            or p.get("symbol") in ("sUSDe", "USDe")
        ) and (p.get("tvlUsd") or 0) > 1_000_000],
        key=lambda p: p.get("tvlUsd") or 0, reverse=True,
    )
    if matches:
        p = matches[0]
        apy = p.get("apy") or p.get("apyBase") or 0
        print(f"    [Ethena DL fallback] Using: project={p.get('project')} symbol={p.get('symbol')} apy={apy}")
        return {"apy": float(apy), "tvl": p.get("tvlUsd") or 0}
    return None


def process_falcon_defillama(pools):
    """Fallback: pull sUSDf APY from DefiLlama when Falcon API is Cloudflare-blocked."""
    matches = sorted(
        [p for p in pools if (
            "falcon" in (p.get("project") or "").lower()
            or p.get("symbol") in ("sUSDf", "USDf")
        )],
        key=lambda p: p.get("tvlUsd") or 0, reverse=True,
    )
    if matches:
        p = matches[0]
        apy = p.get("apy") or p.get("apyBase") or 0
        print(f"    [Falcon DL fallback] Using: project={p.get('project')} symbol={p.get('symbol')} apy={apy}")
        return {"apy": float(apy), "tvl": p.get("tvlUsd"), "staked": None}
    return None


def process_morpho_defillama(pools):
    """Fallback: pull Morpho vault APYs from DefiLlama when Morpho API is blocked.

    DefiLlama vault symbols for Morpho are vault-token names (steakUSDC, gtuUSDC, etc.),
    NOT the underlying asset symbol. Flexible substring matching is required.
    """
    morpho_pools = [p for p in pools if "morpho" in (p.get("project") or "").lower()]
    print(f"    [Morpho DL fallback] {len(morpho_pools)} pools with 'morpho' project found in DefiLlama")
    for p in morpho_pools[:10]:
        print(f"      sample: project={p.get('project')} symbol={p.get('symbol')} "
              f"apy={p.get('apy')} apyBase={p.get('apyBase')} tvl={p.get('tvlUsd')}")

    def underlying_stable(sym):
        """Return 'USDT' or 'USDC' if the vault symbol represents a USD-stablecoin vault."""
        s = (sym or "").upper()
        if "USDT" in s:
            return "USDT"
        if "USDC" in s:
            return "USDC"
        return None

    candidates = []
    for p in morpho_pools:
        asset = underlying_stable(p.get("symbol"))
        if asset is None:
            continue
        if (p.get("tvlUsd") or 0) < 5_000_000:
            continue
        apy = float(p.get("apy") or p.get("apyBase") or 0)
        if not (3.5 <= apy <= 200):
            continue
        candidates.append((p, asset, apy))
    print(f"    [Morpho DL fallback] {len(candidates)} candidates after symbol/tvl/apy filter")

    results = []
    for p, asset, apy in sorted(candidates, key=lambda x: x[0].get("tvlUsd") or 0, reverse=True):
        chain = p.get("chain") or ""
        sym = p.get("symbol", "")
        meta = p.get("poolMeta") or ""
        name = meta if meta else sym if sym else f'Morpho {asset}'
        tvl = float(p.get("tvlUsd") or 0)
        results.append({"name": name, "chain": chain, "asset": asset, "apy": apy, "tvl": tvl})
        print(f"    [Morpho DL fallback] {name} ({chain}) | {asset} | APY={apy:.2f}% | TVL=${tvl/1e6:.1f}M")
    return results


# ── My Positions (tracked wallets) ──────────────────────────────────────────────────
#
# Addresses come from the APY_WALLETS env var (comma-separated; a GitHub Actions secret in CI) or a
# gitignored wallets.local.json (["0x…", "0x…"]) for local runs. They are never written to the repo
# or the page, and are redacted from CI logs. Positions are discovered live each run, so
# a new deposit into any covered protocol shows up automatically and exits drop off.

AAVE_V4_GQL_URL = "https://api.v4.aave.com/graphql"
FLUID_API_URL = "https://api.fluid.instadapp.io/v2/lending/{chain_id}/users/{user}/positions"
LLAMA_PRICES_URL = "https://coins.llama.fi/prices/current/"

POS_MIN_USD = 50  # hide dust (leftover rewards, rounding)

_partial_failures = set()  # sources that answered for some chains but not others (e.g. one Fluid chain timed out)

RPC_URLS = {
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"],
    "arbitrum": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"],
    "base":     ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
}
CHAIN_LABEL = {"ethereum": "Ethereum", "arbitrum": "Arbitrum", "base": "Base"}
CHAIN_ID_LABEL = {1: "Ethereum", 42161: "Arbitrum", 8453: "Base", 137: "Polygon", 9745: "Plasma",
                  43114: "Avalanche", 10: "Optimism", 56: "BNB Chain", 130: "Unichain", 999: "HyperEVM"}

MORPHO_CHAIN_IDS = [1, 8453, 42161]
FLUID_CHAIN_IDS = [1, 42161, 8453, 137, 9745]

# ERC-4626 staking vaults read straight from chain: (chain, vault, protocol, label, apy source)
ERC4626_VAULTS = [
    ("ethereum", "0xc8cf6d7991f15525488b2a83df53468d682ba4b0", "Falcon", "sUSDf", "falcon"),
    ("ethereum", "0x9d39a5de30e57443bff2a8307a4256c8797a3497", "Ethena", "sUSDe", "ethena"),
]

# Uniswap V3 NonfungiblePositionManager + factory per chain
UNI_V3 = {
    "ethereum": ("0xC36442b4a4522E871399CD717aBDD847Ab11FE88", "0x1F98431c8aD98523631AE4a59f267346ea31F984"),
    "arbitrum": ("0xC36442b4a4522E871399CD717aBDD847Ab11FE88", "0x1F98431c8aD98523631AE4a59f267346ea31F984"),
    "base":     ("0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1", "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"),
}

STABLE_SYMBOLS = {"USDC", "USDT", "USDT0", "USD₮0", "USDt", "EURC", "USDG", "USDf", "USDe", "USDS", "DAI",
                  "GHO", "frxUSD", "PYUSD", "RLUSD", "USDH", "USDtb", "crvUSD", "USDC.e", "JUPUSD"}

# 4-byte selectors (stdlib has no keccak, so these are precomputed)
SEL_BALANCE_OF = "0x70a08231"           # balanceOf(address)
SEL_CONVERT_TO_ASSETS = "0x07a2d13a"    # convertToAssets(uint256)
SEL_ASSET = "0x38d52e0f"                # asset()
SEL_DECIMALS = "0x313ce567"             # decimals()
SEL_SYMBOL = "0x95d89b41"               # symbol()
SEL_TOKEN_OF_OWNER = "0x2f745c59"       # tokenOfOwnerByIndex(address,uint256)
SEL_POSITIONS = "0x99fbab88"            # positions(uint256)
SEL_GET_POOL = "0x1698ee82"             # getPool(address,address,uint24)
SEL_SLOT0 = "0x3850c7bd"                # slot0()

MORPHO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://app.morpho.org",
    "Referer": "https://app.morpho.org/",
}


def load_wallets():
    raw = os.environ.get("APY_WALLETS", "").strip()
    if raw:
        addrs = [a.strip() for a in raw.replace("\n", ",").split(",") if a.strip()]
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wallets.local.json")
        try:
            with open(path) as f:
                addrs = json.load(f)
        except (OSError, ValueError):
            addrs = []
    addrs = [a for a in addrs if a.startswith("0x") and len(a) == 42]
    if os.environ.get("GITHUB_ACTIONS"):
        # Logs of a public repo are public: tell Actions to redact the addresses everywhere (the bare
        # 40-hex body also covers checksummed, lowercase and zero-padded log-topic forms).
        for a in addrs:
            for form in {a, a.lower(), a[2:], a[2:].lower()}:
                print(f"::add-mask::{form}")
    return addrs


# ── low-level helpers ──

def _pad_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")

def _pad_int(n):
    return format(n, "x").rjust(64, "0")

def _words(hexdata):
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [h[i:i + 64] for i in range(0, len(h), 64)]

def _int(word, signed=False):
    v = int(word, 16)
    if signed and v >= 2 ** 255:
        v -= 2 ** 256
    return v

def _decode_string(hexdata):
    w = _words(hexdata)
    if not w: return ""
    try:
        if len(w) >= 3:  # dynamic string: offset, length, data
            ln = _int(w[1])
            return bytes.fromhex("".join(w[2:])[:ln * 2]).decode("utf-8", "ignore")
        return bytes.fromhex(w[0]).rstrip(b"\x00").decode("utf-8", "ignore")  # bytes32 symbols (old tokens)
    except ValueError:
        return ""


def rpc_batch(chain, calls):
    """calls = [(to, data), …] → list of hex results (None on revert). Tries each RPC in turn."""
    if not calls: return []
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]} for i, (to, data) in enumerate(calls)]
    last_err = None
    for url in RPC_URLS[chain]:
        try:
            resp = fetch_json(url, method="POST", data=payload, retries=2)
            if not isinstance(resp, list):
                raise ValueError(f"unexpected RPC response: {str(resp)[:120]}")
            out = [None] * len(calls)
            for r in resp:
                res = r.get("result")
                out[r["id"]] = res if res and res != "0x" else None
            return out
        except Exception as e:
            last_err = e
    raise last_err


def gql(url, query, variables=None, headers=None):
    resp = fetch_json(url, method="POST", data={"query": query, "variables": variables or {}}, headers=headers)
    if resp.get("errors"):
        raise ValueError(resp["errors"][0].get("message", "GraphQL error"))
    return resp["data"]


def llama_prices(keys):
    """keys like 'arbitrum:0x…' → {key: price}."""
    keys = sorted(set(keys))
    if not keys: return {}
    coins = fetch_json(LLAMA_PRICES_URL + ",".join(keys)).get("coins", {})
    return {k.lower(): v.get("price") for k, v in coins.items()}


def _pos(wallet, protocol, name, chain, asset, usd, apy, group=None, detail="", token=None):
    if group is None:
        group = "stable" if asset in STABLE_SYMBOLS else "volatile"
    return {"wallet": wallet, "protocol": protocol, "name": name, "chain": chain, "asset": asset,
            "usd": float(usd), "apy": apy, "group": group, "detail": detail,
            "token": token.lower() if token else None}  # receipt token, so auto-discovery skips it


def _net_lending_rows(wallet, protocol, name, chain, supplies, borrows):
    """Collapse one wallet's supplies + borrows in a single Aave market/spoke into rows.
    supplies/borrows: [(symbol, usd, apy%)]. Without debt each supply is its own row; with debt the
    market becomes one net row (equity, net APY on equity)."""
    if not borrows:
        return [_pos(wallet, protocol, name, chain, sym, usd, apy) for sym, usd, apy in supplies]
    s_usd = sum(u for _, u, _ in supplies)
    d_usd = sum(u for _, u, _ in borrows)
    net = s_usd - d_usd
    if net <= 0: return []
    net_apy = (sum(u * a for _, u, a in supplies) - sum(u * a for _, u, a in borrows)) / net
    s_syms = "+".join(sym for sym, _, _ in supplies)
    b_syms = "+".join(sym for sym, _, _ in borrows)
    s_apy = sum(u * a for _, u, a in supplies) / s_usd if s_usd else 0
    b_apy = sum(u * a for _, u, a in borrows) / d_usd if d_usd else 0
    detail = f"{s_syms} supply {s_apy:.2f}% &middot; {b_syms} borrow &minus;{b_apy:.2f}%"
    group = "stable" if all(sym in STABLE_SYMBOLS for sym, _, _ in supplies + borrows) else "volatile"
    return [_pos(wallet, protocol, name, chain, f"{s_syms} / {b_syms}", net, net_apy, group, detail)]


# ── per-protocol position fetchers (each returns a list of position rows for one wallet) ──

AAVE_V3_USER_QUERY = """query($m:[MarketInput!]!,$u:EvmAddress!){
  userSupplies(request:{markets:$m, user:$u}){ market{ name chain{ name } } currency{ symbol } balance{ usd } apy{ value } }
  userBorrows(request:{markets:$m, user:$u}){ market{ name chain{ name } } currency{ symbol } debt{ usd } apy{ value } }
}"""

def pos_aave_v3(wallet_idx, addr, aave_markets):
    markets = [{"address": m["address"], "chainId": m["chain"]["chainId"]}
               for m in aave_markets or [] if m.get("address") and (m.get("chain") or {}).get("chainId")]
    if not markets:
        raise ValueError("no Aave V3 market list")
    d = gql(AAVE_GQL_URL, AAVE_V3_USER_QUERY, {"m": markets, "u": addr})
    by_market = {}
    for kind, key in (("s", "userSupplies"), ("b", "userBorrows")):
        for r in d.get(key) or []:
            chain = r["market"]["chain"]["name"]
            mk = (r["market"]["name"], chain)
            usd = float((r.get("balance") or r.get("debt"))["usd"])
            by_market.setdefault(mk, {"s": [], "b": []})[kind].append(
                (r["currency"]["symbol"], usd, float(r["apy"]["value"]) * 100))
    rows = []
    for (mname, chain), sb in by_market.items():
        sub = _aave_market_label(mname, chain)
        rows += _net_lending_rows(wallet_idx, "Aave V3", sub or "Core", chain, sb["s"], sb["b"])
    return rows


_AAVE_V4_RES = """reserve{ spoke{ name chain{ name } } asset{ underlying{ info{ symbol } } }
  summary{ supplyApy{ normalized } borrowApy{ normalized }
    rewards{ __typename ... on MerklSupplyReward { extraApy{ normalized } } ... on MerklBorrowReward { discountApy{ normalized } } } } }"""
AAVE_V4_USER_QUERY = """query($u:EvmAddress!,$c:[ChainId!]!){
  userSupplies(request:{query:{userChains:{user:$u, chainIds:$c}}}){ balance{ exchange(currency:USD){ value } } %s }
  userBorrows(request:{query:{userChains:{user:$u, chainIds:$c}}}){ debt{ exchange(currency:USD){ value } } %s }
}""" % (_AAVE_V4_RES, _AAVE_V4_RES)

_aave_v4_chain_cache = []

def _aave_v4_chain_ids():
    if not _aave_v4_chain_cache:
        d = gql(AAVE_V4_GQL_URL, "{ chains(request:{query:{filter:MAINNET_ONLY}}) { chainId } }")
        _aave_v4_chain_cache.extend(c["chainId"] for c in d["chains"])
    return _aave_v4_chain_cache

def pos_aave_v4(wallet_idx, addr):
    d = gql(AAVE_V4_GQL_URL, AAVE_V4_USER_QUERY, {"u": addr, "c": _aave_v4_chain_ids()})
    by_spoke = {}
    for kind, key, amt in (("s", "userSupplies", "balance"), ("b", "userBorrows", "debt")):
        for r in d.get(key) or []:
            res = r["reserve"]
            summ = res["summary"]
            if kind == "s":
                apy = float(summ["supplyApy"]["normalized"]) + sum(
                    float(x["extraApy"]["normalized"]) for x in summ.get("rewards") or [] if x.get("extraApy"))
            else:
                apy = float(summ["borrowApy"]["normalized"]) - sum(
                    float(x["discountApy"]["normalized"]) for x in summ.get("rewards") or [] if x.get("discountApy"))
            sk = (res["spoke"]["name"], res["spoke"]["chain"]["name"])
            by_spoke.setdefault(sk, {"s": [], "b": []})[kind].append(
                (res["asset"]["underlying"]["info"]["symbol"], float(r[amt]["exchange"]["value"]), apy))
    rows = []
    for (spoke, chain), sb in by_spoke.items():
        rows += _net_lending_rows(wallet_idx, "Aave V4", f"{spoke} spoke", chain, sb["s"], sb["b"])
    return rows


MORPHO_USER_QUERY = """query($a:String!,$c:Int!){ userByAddress(address:$a, chainId:$c){
  vaultV2Positions{ assetsUsd vault{ address name netApy asset{ symbol } } }
  vaultPositions{ state{ assetsUsd } vault{ address name asset{ symbol } state{ netApy } } }
} }"""

def pos_morpho(wallet_idx, addr):
    rows = []
    for cid in MORPHO_CHAIN_IDS:
        try:
            u = gql(MORPHO_GQL_URL, MORPHO_USER_QUERY, {"a": addr, "c": cid}, MORPHO_HEADERS)["userByAddress"]
        except ValueError as e:
            if "No results" in str(e) or "not found" in str(e).lower():
                continue  # address never used Morpho on this chain
            raise
        chain = CHAIN_ID_LABEL.get(cid, str(cid))
        for p in (u or {}).get("vaultV2Positions") or []:
            v = p["vault"]
            rows.append(_pos(wallet_idx, "Morpho", v["name"], chain, v["asset"]["symbol"],
                             p.get("assetsUsd") or 0, (v.get("netApy") or 0) * 100, token=v.get("address")))
        for p in (u or {}).get("vaultPositions") or []:
            v = p["vault"]
            rows.append(_pos(wallet_idx, "Morpho", v["name"], chain, v["asset"]["symbol"],
                             (p.get("state") or {}).get("assetsUsd") or 0, ((v.get("state") or {}).get("netApy") or 0) * 100,
                             token=v.get("address")))
    return rows


MAPLE_USER_QUERY = """query($id:ID!){ account(id:$id){ poolV2Positions{ lendingBalance
  pool{ id name spotApy asset{ symbol decimals price } } } } }"""

def pos_maple(wallet_idx, addr):
    acct = gql(MAPLE_GQL_URL, MAPLE_USER_QUERY, {"id": addr.lower()}).get("account") or {}
    rows = []
    for p in acct.get("poolV2Positions") or []:
        pool = p["pool"]
        a = pool["asset"]
        amount = int(p.get("lendingBalance") or 0) / 10 ** int(a["decimals"])
        price = float(a.get("price") or 0) / 1e8 if a.get("price") else 1.0  # Maple prices are 8-dec
        if not (0.5 < price < 2): price = 1.0  # USD stables only on Maple; guard against unit drift
        rows.append(_pos(wallet_idx, "Maple", pool["name"], "Ethereum", a["symbol"],
                         amount * price, float(pool.get("spotApy") or 0) / 1e28, token=pool.get("id")))
    return rows


def _llama_apy(pools, project, chain, token_addr):
    token_addr = token_addr.lower()
    m = [p for p in pools if p.get("project") == project and p.get("chain") == chain
         and token_addr in [t.lower() for t in p.get("underlyingTokens") or []]]
    if not m: return None
    p = max(m, key=lambda p: p.get("tvlUsd") or 0)
    return p.get("apy")

def pos_fluid(wallet_idx, addr, pools):
    rows = []
    for cid in FLUID_CHAIN_IDS:
        try:
            data = fetch_json(FLUID_API_URL.format(chain_id=cid, user=addr), retries=2).get("data") or []
        except Exception as e:
            print(f"    [Positions] Fluid chain {cid} unavailable: {e}")
            if cid in (1, 42161, 8453):  # main chains — a miss here can hide a real position
                _partial_failures.add("Fluid")
            continue
        chain = CHAIN_ID_LABEL.get(cid, str(cid))
        for p in data:
            if int(p.get("shares") or 0) == 0: continue
            t = p["token"]
            a = t["asset"]
            amount = int(p.get("underlyingAssets") or 0) / 10 ** int(a["decimals"])
            usd = amount * float(a.get("price") or 0)
            # DefiLlama includes Merkl rewards the Fluid API misses; fall back to Fluid's own rate
            apy = _llama_apy(pools, "fluid-lending", chain, a["address"])
            if apy is None:
                apy = int(t.get("totalRate") or 0) / 100
            sym = a["symbol"]
            sym = "USDT" if sym in ("USD₮0", "USDT0") else sym
            rows.append(_pos(wallet_idx, "Fluid", f"f{sym}", chain, sym, usd, apy, token=t.get("address")))
    return rows


def pos_erc4626(wallet_idx, addr, apys):
    rows = []
    by_chain = {}
    for v in ERC4626_VAULTS:
        by_chain.setdefault(v[0], []).append(v)
    for chain, vaults in by_chain.items():
        res = rpc_batch(chain, [(v[1], SEL_BALANCE_OF + _pad_addr(addr)) for v in vaults]
                               + [(v[1], SEL_ASSET) for v in vaults])
        n = len(vaults)
        held = []
        for i, v in enumerate(vaults):
            shares = _int(res[i]) if res[i] else 0
            if shares and res[n + i]:
                held.append((v, shares, "0x" + _words(res[n + i])[0][24:]))
        if not held: continue
        res2 = rpc_batch(chain, [(v[1], SEL_CONVERT_TO_ASSETS + _pad_int(sh)) for v, sh, _ in held]
                                + [(asset, SEL_DECIMALS) for _, _, asset in held])
        prices = llama_prices([f"{chain}:{asset}" for _, _, asset in held])
        for i, (v, sh, asset) in enumerate(held):
            dec = _int(res2[len(held) + i]) if res2[len(held) + i] else 18
            amount = _int(res2[i]) / 10 ** dec if res2[i] else 0
            price = prices.get(f"{chain}:{asset}".lower()) or 1.0
            _, _, protocol, label, apy_key = v
            rows.append(_pos(wallet_idx, protocol, label, CHAIN_LABEL[chain], label, amount * price,
                             apys.get(apy_key), "stable", token=v[1]))
    return rows


def _uni_amounts(liquidity, sqrt_p, tick_lo, tick_hi):
    sa, sb = math.sqrt(1.0001 ** tick_lo), math.sqrt(1.0001 ** tick_hi)
    if sqrt_p <= sa:
        return liquidity * (sb - sa) / (sa * sb), 0.0
    if sqrt_p >= sb:
        return 0.0, liquidity * (sb - sa)
    return liquidity * (sb - sqrt_p) / (sqrt_p * sb), liquidity * (sqrt_p - sa)

def pos_uniswap_v3(wallet_idx, addr, pools):
    rows = []
    for chain, (npm, factory) in UNI_V3.items():
        n = rpc_batch(chain, [(npm, SEL_BALANCE_OF + _pad_addr(addr))])[0]
        count = _int(n) if n else 0
        if not count: continue
        ids = rpc_batch(chain, [(npm, SEL_TOKEN_OF_OWNER + _pad_addr(addr) + _pad_int(i)) for i in range(min(count, 50))])
        ids = [_int(x) for x in ids if x]
        infos = rpc_batch(chain, [(npm, SEL_POSITIONS + _pad_int(t)) for t in ids])
        live = []
        for tid, info in zip(ids, infos):
            if not info: continue
            w = _words(info)
            liq = _int(w[7])
            if liq == 0: continue  # closed position NFT
            live.append({"id": tid, "t0": "0x" + w[2][24:], "t1": "0x" + w[3][24:], "fee": _int(w[4]),
                         "lo": _int(w[5], True), "hi": _int(w[6], True), "liq": liq})
        if not live: continue
        tokens = sorted({p["t0"] for p in live} | {p["t1"] for p in live})
        meta = rpc_batch(chain, [(t, SEL_DECIMALS) for t in tokens] + [(t, SEL_SYMBOL) for t in tokens])
        dec = {t: _int(meta[i]) if meta[i] else 18 for i, t in enumerate(tokens)}
        sym = {t: _decode_string(meta[len(tokens) + i]) if meta[len(tokens) + i] else "?" for i, t in enumerate(tokens)}
        pool_addrs = rpc_batch(chain, [(factory, SEL_GET_POOL + _pad_addr(p["t0"]) + _pad_addr(p["t1"]) + _pad_int(p["fee"])) for p in live])
        slots = rpc_batch(chain, [("0x" + _words(pa)[0][24:], SEL_SLOT0) for pa in pool_addrs])
        prices = llama_prices([f"{chain}:{t}" for t in tokens])
        for p, slot in zip(live, slots):
            if not slot: continue
            w = _words(slot)
            sqrt_p = _int(w[0]) / 2 ** 96
            tick = _int(w[1], True)
            a0, a1 = _uni_amounts(p["liq"], sqrt_p, p["lo"], p["hi"])
            a0 /= 10 ** dec[p["t0"]]
            a1 /= 10 ** dec[p["t1"]]
            usd = a0 * (prices.get(f"{chain}:{p['t0']}".lower()) or 0) + a1 * (prices.get(f"{chain}:{p['t1']}".lower()) or 0)
            in_range = p["lo"] <= tick < p["hi"]
            fee_tag = f"{p['fee'] / 10000:g}%"
            pair = f"{sym[p['t0']]}/{sym[p['t1']]}"
            apy = None
            if in_range:
                want = {p["t0"].lower(), p["t1"].lower()}
                m = [x for x in pools if x.get("project") == "uniswap-v3" and x.get("chain") == CHAIN_LABEL[chain]
                     and {t.lower() for t in x.get("underlyingTokens") or []} == want and x.get("poolMeta") == fee_tag]
                if m:
                    best = max(m, key=lambda x: x.get("tvlUsd") or 0)
                    apy = best.get("apyBase7d") or best.get("apyBase") or best.get("apy")
            else:
                apy = 0.0
            detail = ("In range &middot; pool-wide 7d fee APR (your range may earn more or less)" if in_range
                      else "Out of range &mdash; earning no fees")
            group = "stable" if all(sym[t] in STABLE_SYMBOLS for t in (p["t0"], p["t1"])) else "volatile"
            rows.append(_pos(wallet_idx, "Uniswap V3", f"{pair} {fee_tag}", CHAIN_LABEL[chain], pair, usd, apy, group, detail))
    return rows


def pos_hyperliquid(wallet_idx, addr, hlp):
    rows = []
    summ = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "delegatorSummary", "user": addr})
    staked = float(summ.get("delegated") or 0)
    if staked > 0:
        dels = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "delegations", "user": addr}) or []
        vals = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "validatorSummaries"}) or []
        apr_by_val = {}
        for v in vals:
            stats = dict(v.get("stats") or [])
            st = stats.get("month") or stats.get("week") or stats.get("day") or {}
            if st.get("predictedApr") is not None:
                apr_by_val[v["validator"].lower()] = float(st["predictedApr"]) * 100
        tot = sum(float(d["amount"]) for d in dels) or staked
        apr = sum(float(d["amount"]) * apr_by_val.get(d["validator"].lower(), 0) for d in dels) / tot if dels else None
        mids = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "allMids"})
        price = float(mids.get("HYPE") or 0)
        rows.append(_pos(wallet_idx, "Hyperliquid", "HYPE staking", "Hyperliquid", "HYPE", staked * price, apr, "volatile"))
    eqs = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "userVaultEquities", "user": addr}) or []
    for e in eqs:
        eq = float(e.get("equity") or 0)
        if eq < POS_MIN_USD: continue
        va = e["vaultAddress"].lower()
        if va == "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303" and hlp:
            name, apr = "HLP vault", hlp.get("apr")
        else:
            det = fetch_json(HYPERLIQUID_URL, method="POST", data={"type": "vaultDetails", "vaultAddress": va})
            name, apr = det.get("name") or "Vault", (det.get("apr") or 0) * 100
        rows.append(_pos(wallet_idx, "Hyperliquid", name, "Hyperliquid", "USDC", eq, apr, "volatile"))
    return rows


# ── Auto-discovery of new protocols (any ERC-4626 vault token) ──
#
# Catches positions in protocols that have no dedicated fetcher above (Euler, Spark, Yearn, Gearbox,
# Silo, a new Morpho-style curator…): anything that hands you an ERC-4626 vault token.
#   Ethereum:        full token list from Routescan (Blockscout fallback) every run.
#   Arbitrum / Base: no free token-list API, so each run scans that day's incoming Transfer logs and
#                    remembers every token seen in a state file (Actions cache in CI, gitignored locally).
#                    → catches every position opened from the day this went live (2026-10-20).
# APY: DefiLlama when it lists the vault (symbol + underlying match), else the vault's own realized
# 7-day share-price growth read from an archive RPC.

ROUTESCAN_URL = "https://api.routescan.io/v2/network/mainnet/evm/1/address/{user}/erc20-holdings?limit=200"
BLOCKSCOUT_ETH_URL = "https://eth.blockscout.com/api?module=account&action=tokenlist&address={user}"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DISCOVERY_STATE = os.environ.get("POSITIONS_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".positions-state.json")

# Free RPCs that allow topic-filtered eth_getLogs, with their max block range per call (tested 2026-10-20:
# publicnode/drpc/blast/nodies refuse or cap at 10–50 blocks). per_day = blocks per day.
LOG_SCAN = {
    "ethereum": {"rpcs": [("https://gateway.tenderly.co/public/mainnet", 1_000_000)], "per_day": 7_200,
                 "first_days": 400, "max_days": 60},
    "arbitrum": {"rpcs": [("https://gateway.tenderly.co/public/arbitrum", 1_000_000),
                          ("https://arb1.arbitrum.io/rpc", 29_000)], "per_day": 345_600,
                 "first_days": 30, "max_days": 10},
    "base":     {"rpcs": [("https://gateway.tenderly.co/public/base", 1_000)], "per_day": 43_200,
                 "first_days": 1, "max_days": 3},
}
ARCHIVE_RPCS = {
    "ethereum": ["https://eth.drpc.org", "https://rpc.mevblocker.io", "https://gateway.tenderly.co/public/mainnet"],
    "arbitrum": ["https://1rpc.io/arb", "https://arbitrum.drpc.org"],
    "base":     ["https://mainnet.base.org", "https://base.drpc.org"],
}
LLAMA_CHAIN = {"ethereum": "Ethereum", "arbitrum": "Arbitrum", "base": "Base"}
DISCOVERY_MIN_VAULT_TVL = 1_000_000  # spam guard: real vaults hold at least $1M
SEL_NAME = "0x06fdde03"              # name()
SEL_TOTAL_ASSETS = "0x01e1d114"      # totalAssets()
PROJECT_NAMES = {"morpho-blue": "Morpho", "euler-v2": "Euler", "spark-savings": "Spark", "sparklend": "Spark",
                 "yearn-finance": "Yearn", "gearbox": "Gearbox", "silo-v2": "Silo", "fluid-lending": "Fluid",
                 "maple": "Maple", "ethena-usde": "Ethena", "falcon-finance": "Falcon", "sky-lending": "Sky",
                 "aave-v3": "Aave", "compound-v3": "Compound", "pendle": "Pendle", "resolv": "Resolv"}


def _project_name(slug):
    if slug in PROJECT_NAMES: return PROJECT_NAMES[slug]
    return " ".join(w.upper() if (w[:1] == "v" and w[1:].isdigit()) else w.capitalize() for w in slug.split("-"))


def _rpc_raw(urls, method, params):
    last = None
    for url in urls:
        try:
            r = fetch_json(url, method="POST", data={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, retries=2)
            if "error" in r: raise ValueError(str(r["error"])[:160])
            return r["result"]
        except Exception as e:
            last = e
    raise last


def _load_state():
    try:
        with open(DISCOVERY_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        with open(DISCOVERY_STATE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"    [Discovery] couldn't save state: {e}")


def _scan_transfer_logs(chain, wallets, chain_state):
    """Tokens transferred INTO any wallet since the last scanned block. Updates chain_state in place
    (progress is saved chunk by chunk, so a failure part-way only delays the rest to the next run)."""
    cfg = LOG_SCAN[chain]
    urls = [u for u, _ in cfg["rpcs"]]
    latest = int(_rpc_raw(urls, "eth_blockNumber", []), 16) - 5  # small reorg margin
    start = chain_state.get("last_block", latest - cfg["first_days"] * cfg["per_day"]) + 1
    start = max(start, latest - cfg["max_days"] * cfg["per_day"])
    topics = [TRANSFER_TOPIC, None, ["0x" + _pad_addr(w) for w in wallets]]
    found, last_err = set(), None
    for url, chunk in cfg["rpcs"]:
        b = chain_state.get("last_block", start - 1) + 1 if chain_state.get("last_block", -1) >= start else start
        try:
            while b <= latest:
                e = min(b + chunk - 1, latest)
                logs = _rpc_raw([url], "eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e), "topics": topics}])
                found.update(l["address"].lower() for l in logs)
                chain_state["last_block"] = e
                b = e + 1
            last_err = None
            break
        except Exception as ex:
            last_err = ex  # try the next RPC from where this one stopped
    chain_state["tokens"] = sorted(set(chain_state.get("tokens", [])) | found)
    if last_err:
        raise last_err
    return found


def _ethereum_token_list(addr):
    try:
        items = fetch_json(ROUTESCAN_URL.format(user=addr), retries=2).get("items") or []
        return {t["tokenAddress"].lower() for t in items if t.get("tokenAddress")}
    except Exception:
        res = fetch_json(BLOCKSCOUT_ETH_URL.format(user=addr), retries=2).get("result") or []
        return {t["contractAddress"].lower() for t in res if t.get("contractAddress")}


def _realized_apy(chain, vault, vault_dec):
    """Annualized share-price growth over ~7 days (ERC-4626 convertToAssets), from an archive RPC."""
    urls = ARCHIVE_RPCS[chain]
    per_day = LOG_SCAN[chain]["per_day"]
    head = _rpc_raw(urls, "eth_getBlockByNumber", ["latest", False])
    now_blk, now_ts = int(head["number"], 16), int(head["timestamp"], 16)
    old_blk = now_blk - 7 * per_day
    old_ts = int(_rpc_raw(urls, "eth_getBlockByNumber", [hex(old_blk), False])["timestamp"], 16)
    call = {"to": vault, "data": SEL_CONVERT_TO_ASSETS + _pad_int(10 ** vault_dec)}
    p_now = int(_rpc_raw(urls, "eth_call", [call, hex(now_blk)]), 16)
    p_old = int(_rpc_raw(urls, "eth_call", [call, hex(old_blk)]), 16)
    days = (now_ts - old_ts) / 86400
    if p_old <= 0 or days <= 0: return None
    return ((p_now / p_old) ** (365 / days) - 1) * 100


def discover_vaults(wallets, covered, pools):
    """Rows for ERC-4626 positions not already covered by a dedicated fetcher."""
    state = _load_state()
    rows = []
    try:
        for chain in ("ethereum", "arbitrum", "base"):
            cs = state.setdefault(chain, {})
            candidates = set(cs.get("tokens", []))
            try:
                candidates |= _scan_transfer_logs(chain, wallets, cs)
            except Exception as e:
                print(f"    [Discovery] {chain} log scan incomplete: {e}")
                _partial_failures.add("new-protocol scan")
            if chain == "ethereum":
                for a in wallets:
                    try:
                        candidates |= _ethereum_token_list(a)
                    except Exception as e:
                        print(f"    [Discovery] Ethereum token list unavailable: {e}")
                        _partial_failures.add("new-protocol scan")
            candidates -= covered
            if candidates:
                rows += _check_vaults(chain, sorted(candidates), wallets, pools)
    finally:
        _save_state(state)
    return rows


def _check_vaults(chain, tokens, wallets, pools):
    # 1) which candidates are ERC-4626 vaults that a wallet actually holds?
    calls = []
    for t in tokens:
        calls.append((t, SEL_ASSET))
        calls += [(t, SEL_BALANCE_OF + _pad_addr(w)) for w in wallets]
    res = []
    for k in range(0, len(calls), 200):
        res += rpc_batch(chain, calls[k:k + 200])
    step = 1 + len(wallets)
    held = []
    for n, t in enumerate(tokens):
        asset_raw = res[n * step]
        bals = [_int(x) if x else 0 for x in res[n * step + 1:(n + 1) * step]]
        if not asset_raw or len(asset_raw) != 66 or not any(bals): continue
        held.append((t, "0x" + asset_raw[-40:], bals))
    if not held: return []

    # 2) vault + underlying metadata, value of each wallet's shares
    calls = []
    for t, asset, bals in held:
        calls += [(t, SEL_DECIMALS), (t, SEL_SYMBOL), (t, SEL_NAME), (t, SEL_TOTAL_ASSETS),
                  (asset, SEL_DECIMALS), (asset, SEL_SYMBOL)]
        calls += [(t, SEL_CONVERT_TO_ASSETS + _pad_int(b)) for b in bals]
    res = rpc_batch(chain, calls)
    prices = llama_prices([f"{chain}:{asset}" for _, asset, _ in held])
    rows, k = [], 0
    for t, asset, bals in held:
        v_dec, v_sym, v_name, tot, a_dec, a_sym = res[k:k + 6]
        conv = res[k + 6:k + 6 + len(bals)]
        k += 6 + len(bals)
        price = prices.get(f"{chain}:{asset}".lower())
        if not price or not tot or not a_dec: continue  # unpriced underlying → likely spam
        a_dec_i = _int(a_dec)
        if _int(tot) / 10 ** a_dec_i * price < DISCOVERY_MIN_VAULT_TVL: continue
        vsym = _decode_string(v_sym) if v_sym else "?"
        asym = _decode_string(a_sym) if a_sym else "?"
        vname = (_decode_string(v_name) if v_name else "") or vsym
        # APY: DefiLlama listing (vault symbol + underlying) → else realized on-chain growth
        proto, apy, detail = "Other vaults", None, ""
        m = [p for p in pools if p.get("chain") == LLAMA_CHAIN[chain] and (p.get("symbol") or "").upper() == vsym.upper()
             and asset.lower() in [u.lower() for u in p.get("underlyingTokens") or []]]
        if m:
            best = max(m, key=lambda p: p.get("tvlUsd") or 0)
            proto, apy = _project_name(best["project"]), best.get("apy")
        else:
            try:
                apy = _realized_apy(chain, t, _int(v_dec) if v_dec else 18)
                detail = "7-day realized vault yield (on-chain)"
            except Exception as e:
                print(f"    [Discovery] no APY for {vsym} on {chain}: {e}")
        for w, c in enumerate(conv):
            usd = (_int(c) / 10 ** a_dec_i * price) if c else 0
            if usd >= POS_MIN_USD:
                rows.append(_pos(w, proto, vname, CHAIN_LABEL[chain], asym, usd, apy, detail=detail, token=t))
    return rows


def fetch_positions(wallets, aave_markets, pools, apys, hlp):
    """Run every (source × wallet) lookup in parallel. Returns (rows, failed_source_names)."""
    jobs = {}
    for i, addr in enumerate(wallets):
        jobs[("Aave V3", i)] = lambda i=i, a=addr: pos_aave_v3(i, a, aave_markets)
        jobs[("Aave V4", i)] = lambda i=i, a=addr: pos_aave_v4(i, a)
        jobs[("Morpho", i)] = lambda i=i, a=addr: pos_morpho(i, a)
        jobs[("Maple", i)] = lambda i=i, a=addr: pos_maple(i, a)
        jobs[("Fluid", i)] = lambda i=i, a=addr: pos_fluid(i, a, pools)
        jobs[("sUSDf/sUSDe", i)] = lambda i=i, a=addr: pos_erc4626(i, a, apys)
        jobs[("Uniswap V3", i)] = lambda i=i, a=addr: pos_uniswap_v3(i, a, pools)
        jobs[("Hyperliquid", i)] = lambda i=i, a=addr: pos_hyperliquid(i, a, hlp)
    rows, failed = [], set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn): key for key, fn in jobs.items()}
        for fut in as_completed(futs):
            src, i = futs[fut]
            try:
                rows += fut.result()
            except Exception as e:
                print(f"    [Positions] ✗ {src} (wallet {i + 1}): {e}")
                failed.add(src)
    # Second pass: anything else that looks like a yield vault (new protocols). If a dedicated fetcher
    # failed, its tokens aren't in `covered` and may show here instead — still better than missing.
    covered = {r["token"] for r in rows if r.get("token")}
    try:
        rows += discover_vaults(wallets, covered, pools)
    except Exception as e:
        print(f"    [Positions] ✗ new-protocol scan: {e}")
        failed.add("new-protocol scan")
    return [r for r in rows if r["usd"] >= POS_MIN_USD], sorted(failed | _partial_failures)


def merge_positions(rows, n_wallets):
    """Combine the same position across wallets → one row with per-wallet USD columns."""
    merged = {}
    for r in rows:
        key = (r["protocol"], r["name"], r["chain"], r["asset"])
        m = merged.setdefault(key, {**r, "usd_by_wallet": [0.0] * n_wallets, "earn": 0.0, "details": []})
        m["usd_by_wallet"][r["wallet"]] += r["usd"]
        m["earn"] += r["usd"] * (r["apy"] or 0)
        if r["detail"] and r["detail"] not in m["details"]:
            m["details"].append(r["detail"])
    out = []
    for m in merged.values():
        total = sum(m["usd_by_wallet"])
        m["usd"] = total
        has_apy = any(r["apy"] is not None for r in rows
                      if (r["protocol"], r["name"], r["chain"], r["asset"]) == (m["protocol"], m["name"], m["chain"], m["asset"]))
        m["apy"] = m["earn"] / total if total and has_apy else None
        m["earn"] = m["earn"] / 100
        out.append(m)
    return sorted(out, key=lambda m: m["usd"], reverse=True)


# ── HTML generation ─────────────────────────────────────────────────────────────────────

def table_row(cells):
    return "<tr>" + "".join(
        f'<td class="{c.get("cls","")}">{c["v"]}</td>' if isinstance(c, dict) else f"<td>{c}</td>"
        for c in cells
    ) + "</tr>"

def make_table(headers, rows):
    return ("<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead><tbody>"
            + "".join(table_row(r) for r in rows) + "</tbody></table>")


def positions_card(positions, n_wallets, failed):
    """Names, assets and APYs only — balances are used internally to blend APYs but never rendered."""
    if not n_wallets:
        return ""
    if not positions:
        return ('<section class="card wide"><span class="src">Wallets</span>'
                '<h2><span class="icon">&#x1F4BC;</span> My Positions</h2>'
                '<p class="error-msg">Position data unavailable today.</p></section>')

    def blended(rows):
        rated = [r for r in rows if r["apy"] is not None]
        tot = sum(r["usd"] for r in rated)
        return (sum(r["usd"] * r["apy"] for r in rated) / tot) if tot else None

    by_apy = lambda rows: sorted(rows, key=lambda r: r["apy"] if r["apy"] is not None else -1, reverse=True)
    stable = by_apy([r for r in positions if r["group"] == "stable"])
    other = by_apy([r for r in positions if r["group"] != "stable"])

    tiles = f"""<div class="tiles">
    <div class="tile"><div class="tile-label">Stablecoin APY</div><div class="tile-value {apy_class(blended(stable))}">{fmt_apy(blended(stable))}</div><div class="tile-sub">blended &middot; {len(stable)} positions</div></div>
    <div class="tile"><div class="tile-label">Overall APY</div><div class="tile-value {apy_class(blended(positions))}">{fmt_apy(blended(positions))}</div><div class="tile-sub">incl. staking, LP &amp; loans</div></div>
  </div>"""

    def row(r):
        detail = "".join(f'<div class="pos-detail">{d}</div>' for d in r["details"])
        apy_cell = fmt_apy(r["apy"]) if r["apy"] is not None else "&mdash;"
        # Inside a protocol group the protocol name is in the header; Aave rows keep their version (V3/V4)
        version = r["protocol"].split()[1] if r["protocol"].startswith("Aave ") else ""
        label = (f'<span class="pos-ver">{version}</span> ' if version else "") + r["name"]
        return (f'<tr><td class="pos">{label} <span class="chain-tag">{r["chain"]}</span>{detail}</td>'
                f'<td class="asset">{r["asset"]}</td>'
                f'<td class="num {apy_class(r["apy"])}">{apy_cell}</td></tr>')

    # One group per protocol (Aave V3 + V4 together), groups ordered by blended APY
    groups = {}
    for r in positions:
        fam = "Aave" if r["protocol"].startswith("Aave") else r["protocol"]
        groups.setdefault(fam, []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: blended(kv[1]) or -1, reverse=True)

    def group(name, rows):
        n = len(rows)
        meta = f'<span class="group-meta">{n} positions</span>' if n > 1 else ""
        g_apy = blended(rows) if n > 1 else None
        apy_cell = f'<td class="num group-apy">{fmt_apy(g_apy)}</td>' if g_apy is not None else '<td></td>'
        return (f'<tr class="group-row"><td colspan="2">{name}{meta}</td>{apy_cell}</tr>'
                + "".join(row(r) for r in by_apy(rows)))

    table = ('<table class="pos-table"><thead><tr><th>Position</th><th>Asset</th><th class="num">APY</th></tr></thead><tbody>'
             + "".join(group(name, rows) for name, rows in ordered) + '</tbody></table>')

    warn = (f'<p class="error-msg">Couldn&rsquo;t reach {", ".join(failed)} today &mdash; positions there may be missing.</p>'
            if failed else "")
    return f"""<section class="card wide">
  <span class="src">Protocol APIs &middot; on-chain</span>
  <h2><span class="icon">&#x1F4BC;</span> My Positions</h2>
  {tiles}
  <div class="table-wrap">{table}</div>
  {warn}
  <p class="note">Current rates, refreshed daily. APY includes Merkl / reward incentives where the protocol reports them. Loan rows show net APY on equity (collateral yield minus borrow cost). Uniswap APR is the pool&rsquo;s 7-day average. New vault positions in other protocols are picked up automatically. Grouped by protocol; group and summary APYs are size-weighted blends.</p>
</section>
"""


def generate_html(fluid, morpho, maple, jupiter, aave, aave_v4, ethena, sky, hlp, falcon, timestamp,
                  positions=None, n_wallets=0, pos_failed=(), aave_v4_src="Aave API"):
    def tbl_rows(data, cols):
        return [[{"v": fmt_apy(r[c]), "cls": apy_class(r[c])} if c == "apy" else
                 {"v": fmt_tvl(r[c]), "cls": "tvl"} if c == "tvl" else r[c]
                 for c in cols] for r in data]

    fluid_html = make_table(["Chain", "Asset", "APY", "TVL"], tbl_rows(fluid, ["chain","asset","apy","tvl"])) if fluid else '<p class="error-msg">No data</p>'

    morpho_rows = [[f'{r["name"]} <span class="chain-tag">{r["chain"]}</span>', r["asset"],
                    {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
                    {"v": fmt_tvl(r["tvl"]), "cls": "tvl"}] for r in morpho]
    morpho_html = make_table(["Vault", "Asset", "Net APY", "TVL"], morpho_rows) if morpho_rows else '<p class="error-msg">No vaults &ge;3.5% APY right now &mdash; check <a href="https://app.morpho.org/earn" target="_blank" rel="noopener">app.morpho.org</a></p>'

    maple_rows = [[r["name"], r["asset"],
                   {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
                   {"v": fmt_tvl(r["tvl"]), "cls": "tvl"}] for r in maple]
    maple_html = make_table(["Pool", "Asset", "APY", "TVL"], maple_rows) if maple_rows else '<p class="error-msg">No data</p>'

    jupiter_html = make_table(["Asset", "APY", "TVL"], tbl_rows(jupiter, ["asset","apy","tvl"])) if jupiter else '<p class="error-msg">No data</p>'

    aave_rows = [[
        f'{r["chain"]}' + (f' <span class="chain-tag">{r["market"]}</span>' if r["market"] else ''),
        r["asset"],
        {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
        {"v": fmt_tvl(r["tvl"]), "cls": "tvl"},
    ] for r in aave]
    aave_html = make_table(["Market", "Asset", "APY", "TVL"], aave_rows) if aave_rows else '<p class="error-msg">No reserves meet the &ge;4% / &ge;$50M threshold right now</p>'

    aave_v4_rows = [[
        f'{r["chain"]}' + (f' <span class="chain-tag">{r["spoke"]}</span>' if r["spoke"] else '')
        + (' <span class="held-tag">Held</span>' if r.get("held") else ''),
        r["asset"],
        {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
        {"v": fmt_tvl(r["tvl"]), "cls": "tvl"},
    ] for r in aave_v4]
    aave_v4_html = make_table(["Market", "Asset", "APY", "TVL"], aave_v4_rows) if aave_v4_rows else '<p class="error-msg">No pools meet the &ge;$10M / &gt;4% threshold right now</p>'

    def inline(data, apy_key="apy", tvl_key="tvl"):
        if not data: return "No data"
        a = data[apy_key]; cls = apy_class(a)
        return f'APY: <strong class="{cls}">{fmt_apy(a)}</strong> | TVL: {fmt_tvl(data[tvl_key])}'

    ethena_html = inline(ethena)
    sky_html = inline(sky)

    if hlp and hlp.get("apr") is not None:
        hcls = apy_class(hlp["apr"])
        hlp_html = f'TVL: {fmt_tvl(hlp["tvl"])} | 30D APR: <strong class="{hcls}">{fmt_apy(hlp["apr"])}</strong> | Trading vault'
    else:
        hlp_html = "No data"

    if falcon and falcon.get("apy") is not None:
        fcls = apy_class(falcon["apy"])
        falcon_html = f'sUSDf APY: <strong class="{fcls}">{fmt_apy(falcon["apy"])}</strong> | Staked: {fmt_tvl(falcon["staked"])} | Protocol TVL: {fmt_tvl(falcon["tvl"])}'
    else:
        falcon_html = 'No data &mdash; check <a href="https://app.falcon.finance/earn/classic" target="_blank" rel="noopener">app.falcon.finance</a>'

    dubai = timezone(timedelta(hours=4))
    date_display = datetime.now(dubai).strftime("%B %-d, %Y")
    ts_display = timestamp.strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Stablecoin APY Dashboard &mdash; {date_display}</title>
<meta name="description" content="Daily stablecoin yield comparison across DeFi protocols">
<meta property="og:title" content="Stablecoin APY Dashboard">
<meta property="og:description" content="Daily stablecoin yield comparison across top DeFi lending protocols">
<meta property="og:type" content="website">
<style>
:root {{--bg:#f5f5f7;--card-bg:#fff;--text:#1d1d1f;--text-secondary:#86868b;--border:#d2d2d7;--shadow:0 1px 3px rgba(0,0,0,.08),0 4px 12px rgba(0,0,0,.04);--orange:#ff9f0a;--red:#ff3b30;--radius:16px}}
@media(prefers-color-scheme:dark){{:root{{--bg:#000;--card-bg:#1c1c1e;--text:#f5f5f7;--text-secondary:#98989d;--border:#38383a;--shadow:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.2)}}}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;padding:2rem max(1rem,env(safe-area-inset-right)) 2rem max(1rem,env(safe-area-inset-left))}}
.container{{max-width:960px;margin:0 auto}}
header{{text-align:center;margin-bottom:2.5rem}}
header h1{{font-family:"New York","SF Pro Display",Georgia,"Times New Roman",serif;font-size:2rem;font-weight:700;letter-spacing:-.02em;margin-bottom:.25rem}}
header .date{{font-size:1.05rem;color:var(--text-secondary);font-weight:400}}
header .sources{{font-size:.8rem;color:var(--text-secondary);margin-top:.35rem;opacity:.7}}
.grid{{display:grid;grid-template-columns:1fr;gap:1.25rem}}
@media(min-width:640px){{.grid{{grid-template-columns:repeat(2,1fr)}}.card.wide{{grid-column:span 2}}}}
.card{{background:var(--card-bg);border-radius:var(--radius);box-shadow:var(--shadow);padding:1.5rem;border:1px solid var(--border);transition:transform .15s ease;position:relative;min-width:0}}
@media(hover:hover){{.card:hover{{transform:translateY(-2px)}}}}
.card h2{{font-size:1.1rem;font-weight:600;margin-bottom:1rem;display:flex;align-items:center;gap:.4rem}}
.card h2 .icon{{font-size:1.3rem}}
.card .src{{position:absolute;top:.75rem;right:1rem;font-size:.65rem;color:var(--text-secondary);opacity:.5;text-transform:uppercase;letter-spacing:.05em}}
.inline-data{{color:var(--text-secondary);font-size:.95rem;word-break:break-word}}
.inline-data a{{color:var(--text-secondary);text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px}}
.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th{{text-align:left;padding:.5rem .6rem;color:var(--text-secondary);font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border);white-space:nowrap}}
td{{padding:.5rem .6rem;border-bottom:1px solid var(--border);white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
.apy-mid{{color:var(--orange);font-weight:600}}.apy-neg{{color:var(--red);font-weight:600}}.tvl{{color:var(--text-secondary)}}
.chain-tag{{white-space:nowrap;font-size:.72rem;color:var(--text-secondary);background:var(--bg);padding:.1rem .4rem;border-radius:4px;font-weight:500}}
.note{{font-size:.78rem;color:var(--text-secondary);margin-top:.5rem;opacity:.7}}
.error-msg{{color:var(--red);font-size:.85rem;padding:.5rem 0}}
.tiles{{display:grid;grid-template-columns:repeat(2,1fr);gap:.75rem;margin-bottom:1.25rem}}
.tile{{background:var(--bg);border-radius:12px;padding:.9rem 1rem}}
.tile-label{{font-size:.72rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em;font-weight:500}}
.tile-value{{font-size:1.6rem;font-weight:600;letter-spacing:-.01em;margin-top:.15rem;font-variant-numeric:tabular-nums}}
.tile-sub{{font-size:.75rem;color:var(--text-secondary);margin-top:.1rem}}
.pos-table td.pos{{white-space:normal;width:100%}}
.held-tag{{white-space:nowrap;font-size:.68rem;font-weight:600;color:var(--orange);border:1px solid var(--orange);padding:0 .35rem;border-radius:4px;margin-left:.2rem}}
.pos-detail{{font-size:.74rem;color:var(--text-secondary);margin-top:.15rem}}
th.num,td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.pos-table tbody tr.group-row:not(:first-child) td{{border-top:12px solid var(--card-bg)}}
.group-row td{{background:var(--bg);font-size:.9rem;font-weight:600;color:var(--text);padding:.5rem .6rem;border-bottom:none}}
.group-row td:first-child{{border-radius:8px 0 0 8px}}.group-row td:last-child{{border-radius:0 8px 8px 0}}
.group-meta{{font-weight:400;font-size:.75rem;color:var(--text-secondary);margin-left:.5rem}}
.group-apy{{font-size:.8rem;font-weight:500;color:var(--text-secondary)}}
.pos-ver{{font-size:.7rem;font-weight:600;color:var(--text-secondary);border:1px solid var(--border);border-radius:4px;padding:0 .3rem;margin-right:.1rem}}
td:first-child{{white-space:normal}}
@media(max-width:600px){{
  body{{padding-top:1.25rem;padding-bottom:1.25rem}}
  header{{margin-bottom:1.5rem}}
  header h1{{font-size:1.6rem}}
  .grid{{gap:1rem}}
  .card{{padding:1.15rem 1rem}}
  .card .src{{display:none}}
  .card h2{{font-size:1.05rem;margin-bottom:.75rem}}
  table{{font-size:.85rem}}
  th,td{{padding:.5rem .4rem}}
  th:first-child,td:first-child{{padding-left:0}}
  th:last-child,td:last-child{{padding-right:0}}
  .group-row td{{padding:.45rem .5rem}}
  .group-row td:first-child{{padding-left:.5rem}}.group-row td:last-child{{padding-right:.5rem}}
  .tile{{padding:.75rem .85rem}}
  .tile-value{{font-size:1.35rem}}
  .inline-data{{font-size:.9rem}}
}}
footer{{text-align:center;margin-top:2.5rem;font-size:.82rem;color:var(--text-secondary)}}
footer .legend{{margin-top:.4rem;font-size:.75rem;opacity:.6}}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>Stablecoin APY Dashboard</h1>
  <p class="date">{date_display}</p>
  <p class="sources">Updated daily at 9:00 AM Dubai time</p>
</header>
<div class="grid">

{positions_card(positions or [], n_wallets, list(pos_failed))}
<section class="card wide">
  <span class="src">DefiLlama</span>
  <h2><span class="icon">&#x1F535;</span> Fluid Lending</h2>
  <div class="table-wrap">{fluid_html}</div>
  <p class="note">Net APY includes FLUID token rewards where applicable.</p>
</section>

<section class="card">
  <span class="src">Hyperliquid API</span>
  <h2><span class="icon">&#x1F7E3;</span> Hyperliquid HLP Vault</h2>
  <p class="inline-data">{hlp_html}</p>
</section>

<section class="card">
  <span class="src">Falcon API</span>
  <h2><span class="icon">&#x1F7E1;</span> Falcon Finance &mdash; sUSDf</h2>
  <p class="inline-data">{falcon_html}</p>
</section>

<section class="card wide">
  <span class="src">Morpho API</span>
  <h2><span class="icon">&#x1F537;</span> Morpho &mdash; USDC/USDT &ge; 3.5% APY</h2>
  <div class="table-wrap">{morpho_html}</div>
</section>

<section class="card wide">
  <span class="src">Maple API</span>
  <h2><span class="icon">&#x1F7E2;</span> Maple Finance</h2>
  <div class="table-wrap">{maple_html}</div>
</section>

<section class="card wide">
  <span class="src">DefiLlama</span>
  <h2><span class="icon">&#x1FA90;</span> Jupiter Lend (Solana)</h2>
  <div class="table-wrap">{jupiter_html}</div>
</section>

<section class="card wide">
  <span class="src">Aave API</span>
  <h2><span class="icon">&#x1F47B;</span> Aave V3 &mdash; USDC / USDT / EURC</h2>
  <div class="table-wrap">{aave_html}</div>
  <p class="note">Pools with &ge; $50M supplied only; USDC &amp; USDT shown when APY &ge; 4%. EURC on Ethereum &amp; Base always shown.</p>
</section>

<section class="card wide">
  <span class="src">{aave_v4_src}</span>
  <h2><span class="icon">&#x1F47B;</span> Aave V4 &mdash; Stablecoins</h2>
  <div class="table-wrap">{aave_v4_html}</div>
  <p class="note">USDC &amp; USDT pools with &ge; $10M supplied and APY &gt; 4% (incl. Merkl rewards). EURC always shown. Pools marked <span class="held-tag">Held</span> are in My Positions and always shown.</p>
</section>

<section class="card">
  <span class="src">Ethena API</span>
  <h2><span class="icon">&#x1F311;</span> sUSDe (Ethena)</h2>
  <p class="inline-data">{ethena_html}</p>
</section>

<section class="card">
  <span class="src">Sky API</span>
  <h2><span class="icon">&#x1F315;</span> sUSDS (Sky)</h2>
  <p class="inline-data">{sky_html}</p>
</section>

</div>
<footer>
  <span>Last updated: {ts_display}</span>
  <div class="legend">Sources: Protocol APIs (Aave V3 &amp; V4, Maple, Morpho, Ethena, Sky, Falcon, Hyperliquid) &middot; DefiLlama (Fluid, Jupiter) &middot; My Positions: Aave V3/V4, Morpho, Maple, Fluid, Hyperliquid APIs + on-chain reads</div>
</footer>
</div>
</body>
</html>"""


# ── Main ────────────────────────────────────────────────────────────────────────────

def main():
    print("=== Stablecoin APY Dashboard Generator ===\n")
    timestamp = datetime.now(timezone.utc)

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_defillama): "defillama",
            pool.submit(fetch_morpho): "morpho",
            pool.submit(fetch_maple): "maple",
            pool.submit(fetch_ethena): "ethena",
            pool.submit(fetch_sky): "sky",
            pool.submit(fetch_hyperliquid): "hlp",
            pool.submit(fetch_falcon): "falcon",
            pool.submit(fetch_aave): "aave",
            pool.submit(fetch_aave_v4): "aave_v4",
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
                print(f"  ✓ {key}")
            except Exception as e:
                print(f"  ✗ {key}: {e}")
                results[key] = None

    pools = results.get("defillama") or []
    fluid = process_fluid(pools)
    morpho = process_morpho(results.get("morpho") or [])
    maple = process_maple(results.get("maple") or [])
    jupiter = process_jupiter(pools)
    ethena = process_ethena(results.get("ethena") or {})
    sky = process_sky(results.get("sky"))
    hlp = process_hlp(results["hlp"]) if results.get("hlp") else None
    falcon = process_falcon(results["falcon"]) if results.get("falcon") else None
    aave = process_aave(results.get("aave"))

    if not morpho and pools:
        morpho = process_morpho_defillama(pools)
        if morpho:
            print(f"  ↳ Morpho: using DefiLlama fallback ({len(morpho)} vaults, Morpho API blocked)")
    if ethena is None:
        ethena = process_ethena_defillama(pools)
        if ethena:
            print("  ↳ Ethena: using DefiLlama fallback (Ethena API blocked)")
    if falcon is None:
        falcon = process_falcon_defillama(pools)
        if falcon:
            print("  ↳ Falcon: using DefiLlama fallback (Falcon API blocked)")

    wallets = load_wallets()
    positions, pos_failed = [], []
    if wallets:
        print(f"\n  Fetching positions for {len(wallets)} wallet(s)...")
        apys = {"falcon": (falcon or {}).get("apy"), "ethena": (ethena or {}).get("apy")}
        pos_rows, pos_failed = fetch_positions(wallets, results.get("aave"), pools, apys, hlp)
        positions = merge_positions(pos_rows, len(wallets))

    # Aave V4 card: official API (spoke names match My Positions); DefiLlama only as fallback
    held_v4 = {(p["chain"], p["name"][:-len(" spoke")], p["asset"]) for p in positions
               if p["protocol"] == "Aave V4" and p["name"].endswith(" spoke")}
    aave_v4_src = "Aave API"
    aave_v4 = process_aave_v4_official(results.get("aave_v4"), held_v4) if results.get("aave_v4") else []
    if not aave_v4 and pools:
        aave_v4, aave_v4_src = process_aave_v4(pools), "DefiLlama"
        print("  ↳ Aave V4: using DefiLlama fallback")

    print(f"\nData summary:")
    print(f"  Fluid:   {len(fluid)} pools (DefiLlama)")
    print(f"  Morpho:  {len(morpho)} vaults (Morpho API)")
    print(f"  Maple:   {len(maple)} pools (Maple API)")
    print(f"  Jupiter: {len(jupiter)} pools (DefiLlama)")
    print(f"  Ethena:  {'✓ ' + fmt_apy(ethena['apy']) if ethena else '✗'} (Ethena API)")
    print(f"  Sky:     {'✓ ' + fmt_apy(sky['apy']) if sky else '✗'} (Sky API)")
    print(f"  HLP:     {'✓' if hlp else '✗'} (Hyperliquid API)")
    print(f"  Falcon:  {'✓ ' + fmt_apy(falcon['apy']) if falcon else '✗'} (Falcon API)")
    print(f"  Aave:    {len(aave)} reserves shown (Aave API)")
    print(f"  Aave V4: {len(aave_v4)} pools shown ({aave_v4_src})")
    if wallets:
        print(f"  My Positions: {len(positions)} positions" + (f" (unreachable: {', '.join(pos_failed)})" if pos_failed else ""))
    else:
        print("  My Positions: skipped (no APY_WALLETS env / wallets.local.json)")

    html = generate_html(fluid, morpho, maple, jupiter, aave, aave_v4, ethena, sky, hlp, falcon, timestamp,
                         positions, len(wallets), pos_failed, aave_v4_src)
    with open("apy_dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ Dashboard written to apy_dashboard.html")

if __name__ == "__main__":
    main()
