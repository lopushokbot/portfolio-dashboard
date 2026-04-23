#!/usr/bin/env python3
"""Fetch DeFi yield data from multiple APIs and generate a static HTML dashboard.

Data sources:
  - Fluid Lending:    DefiLlama yields API (captures Merkle reward APY that Fluid API misses)
  - Jupiter Lend:     DefiLlama yields API
  - Morpho:           Morpho Blue GraphQL API (blue-api.morpho.org)
  - Maple Finance:    Maple GraphQL API (api.maple.finance)
  - Ethena (sUSDe):   Ethena REST API (app.ethena.fi)
  - Sky (sUSDS):      BlockAnalitica REST API (info-sky.blockanalitica.com)
  - Falcon Finance:   Falcon REST API (api.falcon.finance)
  - Hyperliquid HLP:  Hyperliquid REST API (api.hyperliquid.xyz)
"""

import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ──────────────────────────────────────────────────────────────────

DEFILLAMA_URL = "https://yields.llama.fi/pools"
MORPHO_GQL_URL = "https://blue-api.morpho.org/graphql"
MAPLE_GQL_URL = "https://api.maple.finance/v2/graphql"
ETHENA_YIELD_URL = "https://app.ethena.fi/api/yields/protocol-and-staking-yield"
ETHENA_TVL_URL = "https://app.ethena.fi/api/collateralization/status"
SKY_URL = "https://info-sky.blockanalitica.com/api/v1/overall/"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
FALCON_URL = "https://api.falcon.finance/api/v1/statistics"

FLUID_POOLS = [
    ("fluid-lending", "Ethereum", ["USDC", "USDT"]),
    ("fluid-lending", "Arbitrum", ["USDC", "USD₮0"]),
    ("fluid-lending", "Base", ["USDC", "EURC"]),
    ("venus-flux", "BSC", ["USDC", "USDT"]),
]

JUPITER_STABLES = ["USDC", "JUPUSD", "USDT", "USDS", "EURC"]

MORPHO_QUERY = """{
  vaults(
    first: 50
    orderBy: TotalAssetsUsd
    orderDirection: Desc
    where: { whitelisted: true, totalAssetsUsd_gte: 15000000 }
  ) {
    items {
      name
      chain { network }
      asset { symbol }
      state { netApy totalAssetsUsd }
    }
  }
}"""

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


# ── Data fetching ───────────────────────────────────────────────────────────

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
    return fetch_json(MORPHO_GQL_URL, method="POST", data={"query": MORPHO_QUERY})["data"]["vaults"]["items"]


def fetch_maple():
    print("  Fetching Maple Finance...")
    return fetch_json(MAPLE_GQL_URL, method="POST", data={"query": MAPLE_QUERY})["data"]["poolV2S"]


def fetch_ethena():
    print("  Fetching Ethena...")
    # Use a browser UA — Cloudflare on app.ethena.fi blocks the default bot UA
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    extra = {"User-Agent": browser_ua}
    yields = fetch_json(ETHENA_YIELD_URL, headers=extra)
    try:
        tvl_data = fetch_json(ETHENA_TVL_URL, headers=extra)
    except Exception as e:
        print(f"    Ethena TVL fetch failed (non-critical): {e}")
        tvl_data = {}
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
    # Use a browser UA — Cloudflare on api.falcon.finance blocks the default bot UA
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    return fetch_json(FALCON_URL, headers={"User-Agent": browser_ua})


# ── Data processing ─────────────────────────────────────────────────────────

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
        if net_apy < 3.5 or tvl < 15_000_000: continue
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
        results.append({"name": name, "asset": sym, "apy": spot_apy, "tvl": tvl_usd})
    return sorted(results, key=lambda x: x["tvl"], reverse=True)


def process_jupiter(pools):
    jup = sorted(
        [p for p in pools if p.get("project") == "jupiter-lend" and p.get("symbol") in JUPITER_STABLES],
        key=lambda p: p.get("tvlUsd") or 0, reverse=True,
    )
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
    if apy is not None:
        return {"apy": float(apy), "tvl": float(tvl) if tvl is not None else None}
    return None


def process_ethena_defillama(pools):
    """Fallback: pull sUSDe APY from DefiLlama when Ethena API is Cloudflare-blocked."""
    # Be maximally permissive: match any project containing "ethena" OR symbol sUSDe/USDe,
    # on any chain, with any TVL. Take the highest-TVL result (sUSDe has billions in TVL).
    matches = sorted(
        [p for p in pools
         if "ethena" in (p.get("project") or "").lower()
         or p.get("symbol") in ("sUSDe", "USDe")],
        key=lambda p: p.get("tvlUsd") or 0, reverse=True,
    )
    if matches:
        p = matches[0]
        apy = p.get("apy") or p.get("apyBase") or 0
        print(f"    [Ethena DL fallback] Using: project={p.get('project')} symbol={p.get('symbol')} chain={p.get('chain')} tvl={p.get('tvlUsd')} apy={apy}")
        return {"apy": float(apy), "tvl": p.get("tvlUsd")}
    # Debug: log near-matches so we can diagnose future failures from Actions logs
    near = [p for p in pools if (
        "usde" in (p.get("symbol") or "").lower()
        or "ethena" in (p.get("project") or "").lower()
    )]
    print(f"    [Ethena DL fallback] No match found. {len(near)} near-match(es) in DefiLlama:")
    for p in near[:5]:
        print(f"      project={p.get('project')} symbol={p.get('symbol')} chain={p.get('chain')} tvl={p.get('tvlUsd')} apy={p.get('apy')}")
    return None


def process_falcon_defillama(pools):
    """Fallback: pull sUSDf APY from DefiLlama when Falcon API is Cloudflare-blocked."""
    matches = sorted(
        [p for p in pools
         if "falcon" in (p.get("project") or "").lower()
         or p.get("symbol") in ("sUSDf", "USDf")],
        key=lambda p: p.get("tvlUsd") or 0, reverse=True,
    )
    if matches:
        p = matches[0]
        apy = p.get("apy") or p.get("apyBase") or 0
        print(f"    [Falcon DL fallback] Using: project={p.get('project')} symbol={p.get('symbol')} chain={p.get('chain')} tvl={p.get('tvlUsd')} apy={apy}")
        return {"apy": float(apy), "tvl": p.get("tvlUsd"), "staked": None}
    near = [p for p in pools if (
        "usdf" in (p.get("symbol") or "").lower()
        or "falcon" in (p.get("project") or "").lower()
    )]
    print(f"    [Falcon DL fallback] No match found. {len(near)} near-match(es) in DefiLlama:")
    for p in near[:5]:
        print(f"      project={p.get('project')} symbol={p.get('symbol')} chain={p.get('chain')} tvl={p.get('tvlUsd')} apy={p.get('apy')}")
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


# ── HTML generation ─────────────────────────────────────────────────────────

def table_row(cells):
    return "<tr>" + "".join(
        f'<td class="{c.get("cls","")}">{c["v"]}</td>' if isinstance(c, dict) else f"<td>{c}</td>"
        for c in cells
    ) + "</tr>"

def make_table(headers, rows):
    return ("<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead><tbody>"
            + "".join(table_row(r) for r in rows) + "</tbody></table>")


def generate_html(fluid, morpho, maple, jupiter, ethena, sky, hlp, falcon, timestamp):
    def tbl_rows(data, cols):
        return [[{"v": fmt_apy(r[c]), "cls": apy_class(r[c])} if c == "apy" else
                 {"v": fmt_tvl(r[c]), "cls": "tvl"} if c == "tvl" else r[c]
                 for c in cols] for r in data]

    fluid_html = make_table(["Chain", "Asset", "APY", "TVL"], tbl_rows(fluid, ["chain","asset","apy","tvl"])) if fluid else '<p class="error-msg">No data</p>'

    morpho_rows = [[f'{r["name"]} <span class="chain-tag">{r["chain"]}</span>', r["asset"],
                    {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
                    {"v": fmt_tvl(r["tvl"]), "cls": "tvl"}] for r in morpho]
    morpho_html = make_table(["Vault", "Asset", "Net APY", "TVL"], morpho_rows) if morpho_rows else '<p class="error-msg">No data</p>'

    maple_rows = [[r["name"], r["asset"],
                   {"v": fmt_apy(r["apy"]), "cls": apy_class(r["apy"])},
                   {"v": fmt_tvl(r["tvl"]), "cls": "tvl"}] for r in maple]
    maple_html = make_table(["Pool", "Asset", "APY", "TVL"], maple_rows) if maple_rows else '<p class="error-msg">No data</p>'

    jupiter_html = make_table(["Asset", "APY", "TVL"], tbl_rows(jupiter, ["asset","apy","tvl"])) if jupiter else '<p class="error-msg">No data</p>'

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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stablecoin APY Dashboard &mdash; {date_display}</title>
<meta name="description" content="Daily stablecoin yield comparison across DeFi protocols">
<meta property="og:title" content="Stablecoin APY Dashboard">
<meta property="og:description" content="Daily stablecoin yield comparison across top DeFi lending protocols">
<meta property="og:type" content="website">
<style>
:root {{--bg:#f5f5f7;--card-bg:#fff;--text:#1d1d1f;--text-secondary:#86868b;--border:#d2d2d7;--shadow:0 1px 3px rgba(0,0,0,.08),0 4px 12px rgba(0,0,0,.04);--orange:#ff9f0a;--red:#ff3b30;--radius:16px}}
@media(prefers-color-scheme:dark){{:root{{--bg:#000;--card-bg:#1c1c1e;--text:#f5f5f7;--text-secondary:#98989d;--border:#38383a;--shadow:0 1px 3px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.2)}}}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;-webkit-font-smoothing:antialiased;padding:2rem 1rem}}
.container{{max-width:960px;margin:0 auto}}
header{{text-align:center;margin-bottom:2.5rem}}
header h1{{font-family:"New York","SF Pro Display",Georgia,"Times New Roman",serif;font-size:2rem;font-weight:700;letter-spacing:-.02em;margin-bottom:.25rem}}
header .date{{font-size:1.05rem;color:var(--text-secondary);font-weight:400}}
header .sources{{font-size:.8rem;color:var(--text-secondary);margin-top:.35rem;opacity:.7}}
.grid{{display:grid;grid-template-columns:1fr;gap:1.25rem}}
@media(min-width:640px){{.grid{{grid-template-columns:repeat(2,1fr)}}.card.wide{{grid-column:span 2}}}}
.card{{background:var(--card-bg);border-radius:var(--radius);box-shadow:var(--shadow);padding:1.5rem;border:1px solid var(--border);transition:transform .15s ease;position:relative}}
.card:hover{{transform:translateY(-2px)}}
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
.chain-tag{{font-size:.72rem;color:var(--text-secondary);background:var(--bg);padding:.1rem .4rem;border-radius:4px;font-weight:500}}
.note{{font-size:.78rem;color:var(--text-secondary);margin-top:.5rem;opacity:.7}}
.error-msg{{color:var(--red);font-size:.85rem;padding:.5rem 0}}
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

<section class="card">
  <span class="src">Maple API</span>
  <h2><span class="icon">&#x1F7E2;</span> Maple Finance</h2>
  <div class="table-wrap">{maple_html}</div>
</section>

<section class="card wide">
  <span class="src">DefiLlama</span>
  <h2><span class="icon">&#x1FA90;</span> Jupiter Lend (Solana)</h2>
  <div class="table-wrap">{jupiter_html}</div>
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
  <div class="legend">Sources: Protocol APIs (Maple, Morpho, Ethena, Sky, Falcon, Hyperliquid) &middot; DefiLlama (Fluid, Jupiter)</div>
</footer>
</div>
</body>
</html>"""


# ── Main ────────────────────────────────────────────────────────────────────

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
    if ethena is None:
        ethena = process_ethena_defillama(pools)
        if ethena:
            print("  ↳ Ethena: using DefiLlama fallback (Ethena API blocked)")
    sky = process_sky(results.get("sky"))
    hlp = process_hlp(results["hlp"]) if results.get("hlp") else None
    falcon = process_falcon(results["falcon"]) if results.get("falcon") else None
    if falcon is None:
        falcon = process_falcon_defillama(pools)
        if falcon:
            print("  ↳ Falcon: using DefiLlama fallback (Falcon API blocked)")

    print(f"\nData summary:")
    print(f"  Fluid:   {len(fluid)} pools (DefiLlama)")
    print(f"  Morpho:  {len(morpho)} vaults (Morpho API)")
    print(f"  Maple:   {len(maple)} pools (Maple API)")
    print(f"  Jupiter: {len(jupiter)} pools (DefiLlama)")
    print(f"  Ethena:  {'✓ ' + fmt_apy(ethena['apy']) if ethena else '✗'} (Ethena API)")
    print(f"  Sky:     {'✓ ' + fmt_apy(sky['apy']) if sky else '✗'} (Sky API)")
    print(f"  HLP:     {'✓' if hlp else '✗'} (Hyperliquid API)")
    print(f"  Falcon:  {'✓ ' + fmt_apy(falcon['apy']) if falcon else '✗'} (Falcon API)")

    html = generate_html(fluid, morpho, maple, jupiter, ethena, sky, hlp, falcon, timestamp)
    with open("apy_dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ Dashboard written to apy_dashboard.html")

if __name__ == "__main__":
    main()
