"""Probe ESPN NBA odds + record-based Log5 approach."""
import httpx, json

# NBA odds details
print("=== NBA ODDS ===")
r = httpx.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
data = r.json()
for ev in data.get("events", [])[:3]:
    comp = ev["competitions"][0]
    odds = comp.get("odds", [])
    print(f"\nGame: {ev.get('shortName')}")
    if odds:
        o = odds[0]
        print(f"  Provider: {o.get('provider', {}).get('name')}")
        print(f"  Details: {json.dumps(o.get('details'), indent=2)[:200] if o.get('details') else 'n/a'}")
        print(f"  Home odds: {json.dumps(o.get('homeTeamOdds', {}), indent=2)[:300]}")
        print(f"  Away odds: {json.dumps(o.get('awayTeamOdds', {}), indent=2)[:300]}")
        print(f"  Spread: {o.get('spread')}")
        print(f"  Overunder: {o.get('overUnder')}")
    else:
        print("  No odds")

# NHL check
print("\n\n=== NHL ODDS ===")
r2 = httpx.get("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard")
d2 = r2.json()
for ev in d2.get("events", [])[:2]:
    comp = ev["competitions"][0]
    odds = comp.get("odds", [])
    print(f"\nGame: {ev.get('shortName')}")
    if odds:
        o = odds[0]
        print(f"  Provider: {o.get('provider', {}).get('name')}")
        print(f"  Home odds: {json.dumps(o.get('homeTeamOdds', {}), indent=2)[:300]}")
        print(f"  Away odds: {json.dumps(o.get('awayTeamOdds', {}), indent=2)[:300]}")
    else:
        print("  No odds")
    print(f"  Keys: {list(comp.keys())}")

# The Odds API free check
print("\n\n=== THE ODDS API (free) ===")
r3 = httpx.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": "DEMO"})
print(f"Status: {r3.status_code}")
if r3.status_code == 200:
    sports = r3.json()
    active = [s for s in sports if s.get("active")]
    print(f"Active sports: {len(active)}")
    for s in active[:10]:
        print(f"  {s['key']:35s} {s['title']}")
