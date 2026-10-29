import hashlib,json,sqlite3
from pathlib import Path
from export_legacy_session import canonical
root=Path('console/scripts/verify/artifacts/scale-5173-5175')
cases=json.loads((root/'cases.json').read_text())
for e in cases:
 report=Path(e['session_json']).parents[2]/'verification.json'
 expected=json.loads(report.read_text())['legacy_content_sha256']
 h=hashlib.sha256()
 with sqlite3.connect(e['db']) as db:
  for (body,) in db.execute('SELECT m.payload_json FROM transcript_message_payloads m JOIN transcript_turns t ON m.session_id=t.session_id AND m.turn_id=t.turn_id ORDER BY t.turn_seq,m.ordinal'):
   h.update(json.dumps(canonical(json.loads(body)),sort_keys=True,ensure_ascii=False).encode())
 e['db_content_sha256']=h.hexdigest();e['legacy_content_sha256']=expected
 e['same_content']=h.hexdigest()==expected
 print(e['turns'],e['same_content'],flush=True)
 assert e['same_content']
(root/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
