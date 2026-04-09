"""Quick ESPN API probe to find odds/probability data."""
import httpx, json

# Try NBA
print("=== NBA ===")
r = httpx.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", params={"limit": 2})
data = r.json()
if data.get("events"):
    comp = data["events"][0]["competitions"][0]
    print("Keys:", list(comp.keys()))
    print("Has odds:", "odds" in comp)
    print("Has predictor:", "predictor" in comp)
    c0 = comp["competitors"][0]
    print("Competitor keys:", list(c0.keys()))
    if "statistics" in c0:
        print("Stats:", json.dumps(c0["statistics"][:3], indent=2)[:500])

# Try MLB with ESPN BET odds
print("\n=== MLB with ESPN BET ===")
r2 = httpx.get("https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/events",
               params={"limit": 5})
if r2.status_code == 200:
    d2 = r2.json()
    print("Core API keys:", list(d2.keys()))
    if d2.get("items"):
        print("Item sample:", json.dumps(d2["items"][0], indent=2)[:300])
else:
    print(f"Core API HTTP {r2.status_code}")

# Try ESPN odds endpoint directly
print("\n=== ESPN site odds ===")
r3 = httpx.get("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard")
d3 = r3.json()
ev = d3["events"][0]
comp = ev["competitions"][0]
# Look deeper - sometimes odds are nested
for key in comp.keys():
    val = comp[key]
    if isinstance(val, (list, dict)):
        s = json.dumps(val)
        if "odd" in s.lower() or "prob" in s.lower() or "predict" in s.lower() or "money" in s.lower():
            print(f"  Found in '{key}': {s[:300]}")

# Try the ESPN predictor/BPI endpoint for NBA
print("\n=== NBA BPI rankings ===")
r4 = httpx.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/rankings")
if r4.status_code == 200:
    d4 = r4.json()
    print("Rankings keys:", list(d4.keys()))
else:
    print(f"Rankings HTTP {r4.status_code}")

# Records-based probability estimation
print("\n=== Record-based win prob ===")
r5 = httpx.get("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard")
d5 = r5.json()
for ev in d5["events"][:3]:
    comp = ev["competitions"][0]
    cs = comp["competitors"]
    home = next((c for c in cs if c["homeAway"] == "home"), cs[0])
    away = next((c for c in cs if c["homeAway"] == "away"), cs[1])
    ht = home["team"]["abbreviation"]
    at = away["team"]["abbreviation"]
    hr = home.get("records", [{}])[0].get("summary", "0-0")
    ar = away.get("records", [{}])[0].get("summary", "0-0")
    
    # Parse W-L
    def winpct(rec):
        parts = rec.split("-")
        if len(parts) == 2:
            w, l = int(parts[0]), int(parts[1])
            return w / (w + l) if (w + l) > 0 else 0.5
        return 0.5
    
    hw = winpct(hr)
    aw = winpct(ar)
    # Log5 probability
    if hw > 0 and aw > 0:
        log5 = (hw * (1 - aw)) / (hw * (1 - aw) + aw * (1 - hw))
        print(f"  {at}({ar}) @ {ht}({hr})  → home_wp={log5:.1%}")
