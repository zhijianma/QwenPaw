"""Verify both prebuilt databases contain the same display history."""
import hashlib
import json
import sqlite3
from pathlib import Path

root = Path(__file__).parent / 'artifacts/perf-prebuilt-db'
manifest = {}
for name, table in [('current', 'transcript_message_payloads'),
                    ('baseline', 'transcript_messages')]:
    path = root / (name + '.db')
    with sqlite3.connect(path) as db:
        digest = hashlib.sha256()
        count = 0
        for mid, body in db.execute(
            'SELECT message_id,payload_json FROM ' + table
            + ' ORDER BY message_id'
        ):
            digest.update(json.dumps([mid, json.loads(body)], sort_keys=True,
                                     ensure_ascii=False).encode())
            count += 1
        manifest[name] = {
            'messages': count,
            'turns': db.execute('SELECT count(*) FROM transcript_turns')
            .fetchone()[0],
            'runtime_rows': db.execute('SELECT count(*) FROM session_runtime')
            .fetchone()[0],
            'message_sha256': digest.hexdigest(),
            'db_sha256': hashlib.file_digest(path.open('rb'), 'sha256')
            .hexdigest(),
        }
assert manifest['current']['message_sha256'] == manifest['baseline']['message_sha256']
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest, indent=2))
