import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-stats-project/1.0)"}
url = "https://www.efl.com/competitions/carabao-cup/fixtures/"

resp = requests.get(url, headers=HEADERS, timeout=30)
print(f"Status code: {resp.status_code}")
print(f"Page length: {len(resp.text)} chars")

soup = BeautifulSoup(resp.text, "html.parser")
tables = soup.find_all("table")
print(f"Found {len(tables)} <table> elements")

for i, t in enumerate(tables[:5]):
    rows = t.find_all("tr")
    print(f"\n--- Table {i}: {len(rows)} rows ---")
    if rows:
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        print(f"Header row: {header_cells}")
        if len(rows) > 1:
            second_cells = [c.get_text(strip=True) for c in rows[1].find_all(["th", "td"])]
            print(f"Row 2: {second_cells}")

# Also check for common fixture-list patterns that aren't plain <table> elements
# (some modern sites build fixture lists as <div>/<li> instead of <table>)
possible_fixture_containers = soup.find_all(["div", "li"], class_=lambda c: c and (
    "fixture" in c.lower() or "match" in c.lower() or "event" in c.lower()
))
print(f"\nFound {len(possible_fixture_containers)} div/li elements with fixture/match/event in their class name")
if possible_fixture_containers:
    print("First one's class:", possible_fixture_containers[0].get("class"))
    print("First one's text:", possible_fixture_containers[0].get_text(" ", strip=True)[:300])
