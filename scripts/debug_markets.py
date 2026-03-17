"""Debug: inspect actual Polymarket weather event/market structure."""
import requests

print("Fetching events...")
all_events = []
for offset in range(0, 1000, 100):
    resp = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"active": "true", "closed": "false", "limit": 100, "offset": offset},
        timeout=15,
    )
    batch = resp.json()
    if not batch:
        break
    all_events.extend(batch)

print(f"Total active events: {len(all_events)}")

# Very broad search
keywords = ["temperature", "temp", "weather", "°c", "°f", "rain", "snow",
            "forecast", "high", "cold", "warm", "heat", "degree",
            "seoul", "tel aviv", "shanghai", "london", "tokyo", "nyc"]
weather = []
for e in all_events:
    title = e.get("title", "").lower()
    if any(kw in title for kw in keywords):
        weather.append(e)

print(f"Potential weather events: {len(weather)}")
for e in weather[:20]:
    print(f"  - {e.get('title', '?')}")
    markets = e.get("markets", [])
    if markets:
        print(f"    ({len(markets)} markets)")

# Also sample some titles to understand what's there
print(f"\nSample event titles (first 20):")
for e in all_events[:20]:
    print(f"  - {e.get('title', '?')}")
