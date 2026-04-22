import urllib.request
req = urllib.request.Request(
    'http://localhost:9001/trigger-load',
    data=b'{"duration_seconds": 30, "concurrency": 20, "inject_fault": true, "fault_db_latency_ms": 700}',
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    print(urllib.request.urlopen(req).read().decode())
except Exception as e:
    print(e)
