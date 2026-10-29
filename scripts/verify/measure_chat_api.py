"""Measure serialized history requests on the running test service."""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent / 'artifacts/perf-043d0982'
BASE = 'http://localhost:8003/api/chats/'
CHAT = 'fe092757-705b-4b7d-a16d-cb1c9e488314'
records = []


def get(path):
    start = time.perf_counter()
    with urlopen(Request(BASE + CHAT + path,
                         headers={'X-Agent-Id':'UP3wQD'}), timeout=120) as r:
        raw = r.read()
    elapsed = (time.perf_counter()-start)*1000
    data = json.loads(raw)
    records.append({'path': path, 'ms': elapsed, 'bytes': len(raw),
                    'messages': len(data.get('messages', []))})
    return data


for _ in range(3):
    get('')
    get('/user-messages?limit=40')
page = get('')
for _ in range(12):
    history = page.get('history') or page
    cursor = history.get('next_before') or history.get('before')
    if not cursor:
        print('CURSOR_KEYS', history.keys(), flush=True)
        break
    page = get('/messages?' + urlencode({'before':cursor,'limit':5}))
(ROOT / (sys.argv[1] + '-api.json')).write_text(json.dumps(records,indent=2))
print(json.dumps(records,indent=2))
