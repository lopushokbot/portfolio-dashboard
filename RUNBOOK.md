# Stablecoin APY Dashboard — Runbook

> Read this file before doing ANY work on this project.

## Quick Reference
| Task | Command / Steps |
|------|----------------|
| Generate dashboard locally | `cd /Users/iibot/Documents/ppppp/workspace/apy-dashboard && python scripts/generate_dashboard.py` |
| Deploy manually | Generate, then `git add apy_dashboard.html && git commit && git push` |
| Check GitHub Action | `gh run list -R lopushokbot/portfolio-dashboard --limit 5` |
| View live site | https://lopushokbot.github.io/portfolio-dashboard/apy_dashboard.html |

---

## Task: Daily Dashboard Update

### When to run
- Automatically via GitHub Actions cron at 5:00 UTC (9:00 AM Dubai) every day
- Manually if cron fails or data looks stale

### Prerequisites
- Python 3.x installed
- Internet access (all APIs are public)
- Git access to `lopushokbot/portfolio-dashboard`

### Steps
1. Run the generator script:
   ```bash
   cd /Users/iibot/Documents/ppppp/workspace/portfolio-dashboard
   python scripts/generate_dashboard.py
   ```
2. Script fetches all 8 APIs in parallel using ThreadPoolExecutor
3. Each API call has 3 retries with exponential backoff
4. After generation, script validates all 8 sections have data — retries entire generation up to 3x if any section is missing
5. Output: `apy_dashboard.html` in project root

### Validation
- [ ] Open `apy_dashboard.html` in browser
- [ ] All 8 protocol sections are visible and have data
- [ ] APY values look reasonable (most stablecoins 2-15%, HLP can be higher)
- [ ] "Last updated" timestamp is current
- [ ] No "N/A" or "Error" values (unless a protocol is genuinely down)

### If something goes wrong
| Symptom | Cause | Fix |
|---------|-------|-----|
| Ethena shows N/A | Cloudflare blocking GitHub Actions IP | Run locally and push, or wait for retry |
| Morpho shows N/A | 504 timeout | Re-run — usually transient |
| Maple APY looks like 10^28 | Forgot to divide `spotApy` by 1e28 | Check the Maple parsing code |
| DefiLlama returns empty | API down or rate-limited | Check https://defillama.com/docs/api — usually back within minutes |
| GitHub Action failed | Check workflow logs | `gh run view -R lopushokbot/portfolio-dashboard --log-failed` |

---

## Task: Add a New Protocol

### Steps
1. Find the protocol's public API for yield data
2. In `scripts/generate_dashboard.py`:
   - Add the API URL constant at the top
   - Create a `fetch_{protocol}()` function following existing patterns
   - Add it to the `TASKS` dict in the main execution block
   - Add an HTML section in the `build_html()` function
3. Test locally: `python scripts/generate_dashboard.py`
4. Verify the new section appears with correct data
5. Update this RUNBOOK.md and CLAUDE.md with the new data source
6. Commit and push

---

## Task: Fix a Broken API

### Steps
1. Identify which API is failing from the error output or dashboard (shows N/A)
2. Test the API directly:
   ```bash
   curl -s "API_URL_HERE" | python -m json.tool | head -50
   ```
3. Common fixes:
   - **Changed endpoint**: Update the URL constant
   - **Changed response format**: Update the parsing logic
   - **Rate limited**: Add/increase delay between requests
   - **Requires auth now**: Consider switching to DefiLlama as proxy
4. Test locally, verify all 8 sections work, commit and push

---

## Changelog
| Date | Change |
|------|--------|
| 2026-04-14 | Initial dashboard with 8 protocols, GitHub Actions cron |
| 2026-04-27 | Added Aave V3 card (USDC/USDT ≥ 4% APY, EURC always shown, ≥ $100K supply, all chains). Source: official Aave GraphQL API at `api.v3.aave.com/graphql` — not DefiLlama, gives fresher TVL. |
