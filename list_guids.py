import sys
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
from podcast_index import _api_get

data = _api_get("/episodes/byfeedid", {"id": "6280366", "max": 20})
for item in data.get("items", []):
    print(f"{item.get('guid') or item.get('id')} | {item.get('title')}")
