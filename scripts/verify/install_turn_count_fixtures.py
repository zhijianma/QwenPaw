"""Install only new legacy fixtures and verify the running 5173 backend."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from export_legacy_session import canonical

ROOT = Path(__file__).parent / 'artifacts/fixtures'
ROOT = ROOT / 'fe092757-705b-4b7d-a16d-cb1c9e488314/variants'
WORKSPACE = Path('/Users/mazhijian/.qwenpaw/workspaces/UP3wQD')


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request('http://localhost:8001/api/chats' + path, data=data,
                  headers={'X-Agent-Id': 'UP3wQD',
                           'Content-Type': 'application/json'})
    with urlopen(req, timeout=120) as response:
        return json.load(response)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False,
                               separators=(',', ':')) + '\n')


entries = json.loads((ROOT / 'index.json').read_text())
for entry in entries:
    directory = ROOT / str(entry['turns'])
    legacy = directory / 'legacy-5173'
    source = Path(entry['session_json'])
    destination = WORKSPACE / 'sessions/console' / source.name
    if entry['installed_in_live_service']:
        continue
    assert not destination.exists(), destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as output:
        output.write(source.read_bytes())
    spec = json.loads((legacy / 'chat.json').read_text())
    created = request('', spec)
    assert created['session_id'] == entry['session_id']
    entry['chat_id'] = created['id']
    entry['installed_in_live_service'] = True
    entry['url'] = (
        'http://localhost:5173/chat/' + created['id'] + '?agent=UP3wQD'
    )
    save(ROOT / 'index.json', entries)
    save(legacy / 'chat.json', created)
    fixture_path = directory / 'conversation.json'
    fixture = json.loads(fixture_path.read_text())
    fixture['chat'] = created
    save(fixture_path, fixture)
    response = request('/' + created['id'])
    digest = hashlib.sha256()
    for message in response['messages']:
        digest.update(json.dumps(canonical(message), ensure_ascii=False,
                                 sort_keys=True).encode())
    report = json.loads((legacy / 'verification.json').read_text())
    assert len(response['messages']) == entry['counts']['messages']
    assert digest.hexdigest() == report['legacy_content_sha256']
    assert sum(m['role'] == 'user' for m in response['messages']) == entry['turns']
    report['online_8001_get_chat'] = {
        'passed': True, 'messages': len(response['messages']),
        'content_sha256': digest.hexdigest(), 'url': entry['url'],
    }
    report['notes'] = [n for n in report['notes']
                       if n != 'No live workspace modified.']
    report['notes'].append('Installed as a new session; originals unchanged.')
    save(legacy / 'verification.json', report)
    save(directory / 'manifest.json', entry)
    (legacy / 'LOAD.txt').write_text(
        'Installed and verified through the running 8001 GET chat API.\n'
        + entry['url'] + '\nOriginal conversations remain unchanged.\n')
    print('ONLINE_VERIFIED', entry['turns'], entry['url'], flush=True)
save(ROOT / 'index.json', entries)
