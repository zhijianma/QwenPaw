"""Create nested, complete-turn fixtures from the saved source export."""
import asyncio
import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import export_legacy_session as legacy

ROOT = Path(__file__).parent / 'artifacts/fixtures'
SOURCE = ROOT / 'fe092757-705b-4b7d-a16d-cb1c9e488314'
DEST = SOURCE / 'variants'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False,
                               separators=(',', ':')) + '\n')


async def main():
    source = json.loads((SOURCE / 'conversation.json').read_text())
    turns = {turn['turn_id']: turn for turn in source['turns']}
    DEST.mkdir(exist_ok=True)
    index = []
    for size in (500, 100, 50):
        output = DEST / str(size)
        output.mkdir(exist_ok=True)
        destination = output / 'conversation.json'
        if destination.exists():
            raise FileExistsError(destination)
        fixture = {k: copy.deepcopy(v) for k, v in source.items()
                   if k not in ('turns', 'counts')}
        chat_id = str(uuid4())
        session_id = 'perf-history-' + str(size) + '-' + str(uuid4())
        chat = fixture['chat']
        chat.update(id=chat_id, session_id=session_id,
                    name=f'Perf Same History - {size} Turns', pinned=False,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=datetime.now(timezone.utc).isoformat())
        fixture['session']['session_id'] = session_id
        fixture['source_chat_id'] = source['chat']['id']
        fixture['selection'] = {'mock_turn_start': 1, 'mock_turn_end': size}
        selected = []
        counts = Counter()
        digest = hashlib.sha256()
        for number in range(1, size + 1):
            key = f'perf-history-c06fd4e3-{number:04d}'
            original = turns[key]
            turn = copy.deepcopy(original)
            turn.update(session_id=session_id, turn_seq=number)
            for row in turn['messages']:
                row['session_id'] = session_id
                message = row['message']
                counts['messages'] += 1
                counts['content_blocks'] += len(message['content'])
                counts['user_messages'] += message['role'] == 'user'
                digest.update(json.dumps(message, sort_keys=True,
                                         ensure_ascii=False).encode())
            assert [r['message'] for r in turn['messages']] == [
                r['message'] for r in original['messages']]
            selected.append(turn)
        counts['turns'] = len(selected)
        assert counts['user_messages'] == size
        fixture.update(turns=selected, counts=dict(counts))
        save(destination, fixture)
        legacy.ROOT = output
        await legacy.main()
        report = json.loads((output / 'legacy-5173/verification.json').read_text())
        assert not report['differences'] and not report['explicit_adaptations']
        session_file = Path(report['session_file'])
        entry = {'turns': size, 'chat_id': chat_id, 'session_id': session_id,
                 'counts': dict(counts), 'wire_content_sha256': digest.hexdigest(),
                 'session_json': str(session_file.resolve()),
                 'fixture_json': str(destination.resolve()),
                 'verification': 'passed', 'installed_in_live_service': False}
        save(output / 'manifest.json', entry)
        (output / 'legacy-5173/LOAD.txt').write_text(
            'Target: 5173 / 8001 legacy session JSON.\n'
            'Copy sessions/console JSON into the selected agent workspace;\n'
            'add chat.json to chats.json chats[] without replacing existing records.\n'
            f'URL after import: http://localhost:5173/chat/{chat_id}?agent=UP3wQD\n'
            'Only mock turns 0001 through the selected limit are included.\n'
            'All message payloads match the source export exactly.\n'
            'The legacy reader regenerates transport IDs and metadata wrappers.\n'
            'For rendering comparison; no live workspace was modified.\n')
        index.append(entry)
        save(DEST / 'index.json', index)
        print('VERIFIED', size, dict(counts), chat_id, flush=True)


asyncio.run(main())
