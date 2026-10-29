"""Export a legacy session and verify the real legacy history reader."""
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentscope.message import Msg
from qwenpaw.app.chats.api import get_chat
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.session import SafeJSONSession, session_filename

ROOT = Path(__file__).parent / 'artifacts/fixtures'
ROOT = ROOT / 'fe092757-705b-4b7d-a16d-cb1c9e488314'


def canonical(message):
    """Compare rendered payloads without generated transport fields."""
    blocks = []
    for block in message['content']:
        if block['type'] == 'text':
            blocks.append({'type': 'text', 'text': block['text']})
        else:
            data = dict(block['data'])
            data = {k: v for k, v in data.items() if v is not None}
            if not data.get('call_id'):
                data.pop('call_id', None)
            for key in ('arguments', 'output'):
                if isinstance(data.get(key), str):
                    try:
                        data[key] = json.loads(data[key])
                    except (ValueError, TypeError):
                        pass
            blocks.append({'type': 'data', 'data': data})
    metadata = message.get('metadata') or {}
    times = {}
    for key in ('timestamp', 'finished_at'):
        value = metadata.get(key)
        times[key] = datetime.fromisoformat(value).timestamp() if value else None
    return {'type': message['type'], 'role': message['role'],
            'content': blocks, 'times': times}


async def main():
    fixture = json.loads((ROOT / 'conversation.json').read_text())
    originals = [row['message'] for turn in fixture['turns']
                 for row in turn['messages']]
    context = []
    for message in originals:
        blocks = []
        kind = message['type']
        for block in message['content']:
            if kind == 'message':
                blocks.append({'type': 'text', 'text': block['text']})
            elif kind == 'reasoning':
                blocks.append({'type': 'thinking', 'thinking': block['text']})
            else:
                data = block['data']
                item = {'id': data.get('call_id', ''), 'name': data.get('name', '')}
                if kind == 'plugin_call':
                    arguments = data.get('arguments', '')
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    item.update(type='tool_call', input=arguments)
                elif kind == 'plugin_call_output':
                    output = data.get('output', '')
                    if not isinstance(output, str):
                        output = json.dumps(output, ensure_ascii=False)
                    item.update(type='tool_result', output=output,
                                state=data.get('state', 'success'))
                    if data.get('state'):
                        item['state'] = data['state']
                else:
                    raise ValueError(kind)
                blocks.append(item)
        meta = message.get('metadata') or {}
        raw = dict(name=meta.get('original_name') or message['role'],
                   role=message['role'], id=meta.get('original_id') or message['id'],
                   content=blocks, metadata=meta.get('metadata', {}),
                   created_at=meta['timestamp'], finished_at=meta.get('finished_at'))
        context.append(Msg.model_validate(raw).model_dump(mode='json'))
    chat = ChatSpec.model_validate(fixture['chat'])
    directory = ROOT / 'legacy-5173/sessions' / chat.channel
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / session_filename(chat.session_id, chat.user_id)
    state = {'agent': {'state': {'session_id': chat.session_id,
                                'summary': '', 'context': context}}}
    target.write_text(json.dumps(state, ensure_ascii=False, separators=(',', ':')))
    (ROOT / 'legacy-5173/chat.json').write_text(
        json.dumps(fixture['chat'], ensure_ascii=False, indent=2))

    class Manager:
        async def get_chat(self, chat_id):
            assert chat_id == chat.id
            return chat

    class Tracker:
        async def get_status(self, chat_id):
            return 'idle'

    result = await get_chat(
        chat.id, mgr=Manager(),
        session=SafeJSONSession(save_dir=str(directory.parent)),
        workspace=SimpleNamespace(config=SimpleNamespace(backend='qwenpaw'),
                                  task_tracker=Tracker()),
    )
    served = [m.model_dump(mode='json') for m in result.messages]
    assert len(served) == len(originals), (len(served), len(originals))
    differences = []
    adaptations = []
    source_hash = hashlib.sha256()
    target_hash = hashlib.sha256()
    for index, (original, restored) in enumerate(zip(originals, served)):
        a, b = canonical(original), canonical(restored)
        if original['id'] == 'perf-history-c06fd4e3-1000-tool-output':
            assert 'state' not in a['content'][0]['data']
            assert b['content'][0]['data']['state'] == 'success'
            adaptations.append({
                'id': original['id'], 'field': 'content[0].data.state',
                'source': 'absent', 'legacy': 'success',
                'reason': 'Legacy ToolResultBlock requires a state; source message is completed.'})
            a['content'][0]['data']['state'] = 'success'
        if a != b:
            differences.append({'index': index, 'id': original['id'],
                                'type': original['type']})
        for digest, value in ((source_hash, a), (target_hash, b)):
            digest.update(json.dumps(value, ensure_ascii=False,
                                     sort_keys=True).encode())
    report = {'messages': len(served), 'differences': differences, 'explicit_adaptations': adaptations,
              'adapted_source_content_sha256': source_hash.hexdigest(),
              'legacy_content_sha256': target_hash.hexdigest(),
              'reader': '5173 source SafeJSONSession -> get_chat',
              'session_file': str(target),
              'notes': ['Transport IDs and metadata wrapping differ in legacy reader.',
                        'Original wire data remains in conversation.json.',
                        'No live workspace modified.']}
    (ROOT / 'legacy-5173/verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'differences'}))
    print('differences:', len(differences), differences[:10])
    assert not differences
    (ROOT / 'legacy-5173/LOAD.txt').write_text(
        'Target: QwenPaw 5173 / 8001, legacy SafeJSONSession.\n'
        'Copy sessions/console/<filename>.json under the selected agent workspace.\n'
        'Merge chat.json into chats.json chats[] only when its ID is absent.\n'
        'Do not overwrite an existing session without backing it up.\n'
        'Open /chat/fe092757-705b-4b7d-a16d-cb1c9e488314?agent=UP3wQD.\n'
        'This fixture is for history rendering, not resuming the model context.\n'
        'Transport IDs/metadata wrappers are regenerated by the old reader.\n'
        'One tool result lacks source state; exported as success (completed).\n'
        'See verification.json. Source 5175 DB remains unchanged.\n')


if __name__ == "__main__":
    asyncio.run(main())
