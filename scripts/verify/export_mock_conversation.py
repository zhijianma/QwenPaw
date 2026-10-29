# -*- coding: utf-8 -*-
"""Export the selected mock conversation without changing its database."""

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CHAT_ID = "fe092757-705b-4b7d-a16d-cb1c9e488314"
WORKSPACE = Path("/Users/mazhijian/.qwenpaw/workspaces/UP3wQD")
DATABASE = WORKSPACE / "transcripts/c2/" / (
    "c2f1b6ead8743bf8a635ac3042ca41ed6422489d959e32941d80117eb976d2eb.db"
)
OUTPUT = Path(__file__).parent / "artifacts/fixtures" / CHAT_ID


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main():
    chats = json.loads((WORKSPACE / "chats.json").read_text())
    chat = next(item for item in chats["chats"] if item["id"] == CHAT_ID)
    session_id = chat["session_id"]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / "conversation.json"
    temporary = OUTPUT / "conversation.json.tmp"
    manifest_path = OUTPUT / "manifest.json"
    counts = Counter()
    kinds = Counter()
    blocks = Counter()
    turn_checksums = []
    uri = DATABASE.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        session = dict(connection.execute(
            "SELECT * FROM transcript_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone())
        with temporary.open("w", encoding="utf-8") as output:
            header = {
                "format": "qwenpaw.chat-fixture.v1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "agent_id": "UP3wQD",
                "chat": chat,
                "session": session,
                "scope": "materialized_history",
                "includes_runtime_context": False,
            }
            output.write(encode(header)[:-1] + ',"turns":[\n')
            turns = connection.execute(
                "SELECT * FROM transcript_turns WHERE session_id = ? "
                "ORDER BY turn_seq",
                (session_id,),
            )
            for turn in turns:
                record = dict(turn)
                record["messages"] = []
                rows = connection.execute(
                    "SELECT * FROM transcript_messages "
                    "WHERE session_id = ? AND turn_id = ? ORDER BY ordinal",
                    (session_id, turn["turn_id"]),
                )
                for row in rows:
                    message = json.loads(row["payload_json"])
                    entry = dict(row)
                    del entry["payload_json"]
                    entry["message"] = message
                    record["messages"].append(entry)
                    counts["messages"] += 1
                    counts["content_blocks"] += len(message["content"])
                    kinds[message["type"]] += 1
                    counts["user_messages"] += row["role"] == "user"
                    for block in message["content"]:
                        blocks[block["type"]] += 1
                serialized = encode(record)
                turn_checksums.append({
                    "turn_id": turn["turn_id"],
                    "turn_seq": turn["turn_seq"],
                    "messages": len(record["messages"]),
                    "sha256": hashlib.sha256(
                        serialized.encode("utf-8"),
                    ).hexdigest(),
                })
                if counts["turns"]:
                    output.write(",\n")
                output.write(serialized)
                counts["turns"] += 1
            output.write('\n],"counts":' + encode(dict(counts)) + '}\n')
        expected = connection.execute(
            "SELECT COUNT(*), SUM(json_array_length(payload_json, "
            "'$.content')) FROM transcript_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert (counts["messages"], counts["content_blocks"]) == tuple(expected)
    temporary.replace(destination)
    with destination.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    manifest = {
        "format": "qwenpaw.chat-fixture-manifest.v1",
        "file": destination.name,
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "counts": dict(counts),
        "message_types": dict(kinds),
        "content_types": dict(blocks),
        "turn_checksums": turn_checksums,
        "notes": [
            "Exact export of existing mock history, not regenerated data.",
            "Includes all 1002 persisted turns, including extreme fixtures.",
            "Tool call IDs, timestamps and message metadata are preserved.",
            "No runtime context, credentials or external file bodies included.",
            "For migration and rendering comparison, not agent resumption.",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(encode({key: value for key, value in manifest.items()
                  if key != "turn_checksums"}))


if __name__ == "__main__":
    main()
