import hashlib,json,sqlite3,time
from pathlib import Path
from urllib.request import Request,urlopen
root=Path('console/scripts/verify/artifacts/scale-5173-5175')
old=Path('scripts/verify/artifacts/fixtures/fe092757-705b-4b7d-a16d-cb1c9e488314/variants/index.json')
entries=json.loads(old.read_text())+json.loads((root/'fixtures/index.json').read_text())
workspace=Path('/Users/mazhijian/.qwenpaw/workspaces/UP3wQD')
for e in sorted(entries,key=lambda e:e['turns']):
 url='http://localhost:8003/api/chats/'+e['chat_id']
 for attempt in range(2):
  with urlopen(Request(url,headers={'X-Agent-Id':'UP3wQD'}),timeout=180) as r:
   page=json.load(r)
 key=hashlib.sha256(json.dumps(['console','default',e['session_id']],separators=(',',':')).encode()).hexdigest()
 db=workspace/'transcripts'/key[:2]/(key+'.db')
 dest=root/(str(e['turns'])+'-5175.db')
 with sqlite3.connect(db) as src,sqlite3.connect(dest) as dst:
  src.backup(dst)
  turns=src.execute('SELECT count(*) FROM transcript_turns').fetchone()[0]
  messages=src.execute('SELECT count(*) FROM transcript_messages').fetchone()[0]
  assert turns==e['turns'],(turns,e['turns'])
  assert messages==e['counts']['messages']
 e['db']=str(dest.resolve());e['live_db']=str(db)
 print('PREBUILT',e['turns'],messages,flush=True)
(root/'cases.json').write_text(json.dumps(entries,ensure_ascii=False,indent=2))
