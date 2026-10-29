import ast
import json
import sqlite3
import subprocess
from pathlib import Path

root = Path('scripts/verify/artifacts/perf-prebuilt-db')
source = Path('/Users/mazhijian/.qwenpaw/workspaces/UP3wQD/transcripts/c2/c2f1b6ead8743bf8a635ac3042ca41ed6422489d959e32941d80117eb976d2eb.db')
backup = root / 'current.db'
with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
    src.backup(dst)
code = subprocess.check_output(['git', 'show', 'HEAD:src/qwenpaw/app/chats/transcript.py'], text=True)
tree = ast.parse(code)
method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_create_schema')
schema = next(n.args[0].value for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'executescript')
with sqlite3.connect(backup) as src, sqlite3.connect(root / 'baseline.db') as dst:
    dst.executescript(schema)
    for table in ('transcript_sessions', 'transcript_turns', 'session_runtime', 'transcript_messages'):
        columns = [r[1] for r in dst.execute('PRAGMA table_info(' + table + ')')]
        projection = 'transcript_message_payloads' if table == 'transcript_messages' else table
        rows = src.execute('SELECT ' + ','.join(columns) + ' FROM ' + projection)
        dst.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in columns) + ')', rows)
        print(table, dst.execute('SELECT count(*) FROM ' + table).fetchone()[0], flush=True)
    dst.commit()
(root / 'db-path.txt').write_text(str(source))
print('Backed up and prepared equivalent baseline database', flush=True)
