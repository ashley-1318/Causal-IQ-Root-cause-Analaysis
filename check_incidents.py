import urllib.request
import json
try:
    resp = urllib.request.urlopen("http://localhost:9001/incidents?limit=5")
    data = json.loads(resp.read().decode())
    print(json.dumps(data, indent=2))
except Exception as e:
    print("Error:", e)
