"""Quick script to check what weather markets exist on Polymarket."""
import requests
import json

# Try different Gamma API approaches
print("=== Method 1: /events with weather slug ===")
for slug in ["weather", "temperature", "climate"]:
    resp = requests.get(
        f"https://gamma-api.polymarket.com/events",
        params={"slug": slug, "active": "true", "limit": 10},
        timeout=15,
    )
    data = resp.json()
    print(f"slug={slug}: {len(data)} results")
    for e in data[:3]:
        print(f"  - {e.get('title', '?')}")

print()
print("=== Method 2: /markets with tag_slug ===")
for tag in ["weather", "science", "climate"]:
    resp = requests.get(
        f"https://gamma-api.polymarket.com/markets",
        params={"tag_slug": tag, "active": "true", "closed": "false", "limit": 10},
        timeout=15,
    )
    data = resp.json()
    print(f"tag_slug={tag}: {len(data)} results")
    for m in data[:3]:
        print(f"  - {m.get('question', '?')}")

print()
print("=== Method 3: Search events endpoint ===")
resp = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"active": "true", "closed": "false", "limit": 100, "order": "volume24hr", "ascending": "false"},
    timeout=15,
)
events = resp.json()
print(f"Total active events (by volume): {len(events)}")
weather_keywords = ["weather", "temperature", "high", "degrees", "fahrenheit", "celsius", "forecast", "snow", "rain"]
for e in events:
    title = e.get("title", "").lower()
    if any(kw in title for kw in weather_keywords):
        print(f"  MATCH: {e.get('title', '?')}")
        markets = e.get("markets", [])
        for m in markets[:3]:
            q = m.get("question", "")
            print(f"    -> {q}")

print()
print("=== Method 4: Direct text search ===")
resp = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"active": "true", "closed": "false", "limit": 200},
    timeout=15,
)
events = resp.json()
count = 0
for e in events:
    title = e.get("title", "").lower()
    if any(kw in title for kw in weather_keywords):
        count += 1
        print(f"  {e.get('title', '?')}")
print(f"Weather-related events in top 200: {count}")
