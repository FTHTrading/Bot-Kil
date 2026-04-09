import json, pathlib, glob

log_files = sorted(glob.glob(r'C:\Users\Kevan\kalishi-edge\logs\autonomous_*.jsonl'))
print(f"Log files: {log_files}")

all_sessions = []
all_bets = []
for lf in log_files:
    with open(lf) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                all_sessions.append(s)
                for b in s.get('session_log', []):
                    b['_session'] = s.get('session', '?')
                    b['_time'] = s.get('timestamp', '')
                    all_bets.append(b)
            except Exception as e:
                print(f"Parse error: {e}")

print(f"\nTotal sessions completed: {len(all_sessions)}")
print(f"Total bets placed: {len(all_bets)}")
total_cost = 0
for b in all_bets:
    cost = b.get('cost_usd', 0) or 0
    total_cost += cost
    ticker = b.get('ticker','?')
    side = b.get('side','?')
    n = b.get('contracts','?')
    price = b.get('yes_price','?')
    reason = (b.get('reasoning','') or '')[:70]
    print(f"  S{b['_session']} | {ticker} {side} x{n} @{price}c ~${cost:.2f} | {reason}")

print(f"\nTotal agent cost: ${total_cost:.2f}")
