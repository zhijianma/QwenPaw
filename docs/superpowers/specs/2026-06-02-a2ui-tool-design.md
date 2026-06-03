# a2ui Tool Design Spec

## Overview

`a2ui` is a single Agent tool for pushing rich visual content to the Console Web UI. The Agent calls `a2ui(blocks=[...])` whenever it wants to show something beyond plain text — diffs, images, code, cards, tables, etc.

No blocking. No waiting. Agent calls it, UI renders it, Agent moves on.

## Tool Interface

```python
async def a2ui(
    blocks: list[dict],
    title: str = "",
) -> ToolResponse
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `blocks` | `list[dict]` | required | Ordered list of UI blocks to render |
| `title` | `str` | `""` | Optional heading above the block group |

Returns immediately: `"Displayed to user."`.

## Block Types

### `text` — Markdown

```json
{"type": "text", "content": "## Results\n\nFound **3** issues."}
```

### `diff` — Code Diff

```json
{
  "type": "diff",
  "file": "src/main.py",
  "language": "python",
  "old_content": "def foo():\n    pass",
  "new_content": "def foo(x: int) -> int:\n    return x + 1"
}
```

### `code` — Syntax Highlighted

```json
{"type": "code", "language": "python", "content": "print('hello')", "filename": "example.py"}
```

### `image` — Image

```json
{"type": "image", "url": "/path/to/img.png", "alt": "screenshot"}
```

Local paths are converted to `file://` URLs (same as `send_file_to_user`).

### `table` — Table

```json
{
  "type": "table",
  "headers": ["Name", "Status", "Score"],
  "rows": [["Alice", "Pass", "95"], ["Bob", "Fail", "42"]]
}
```

### `card` — Info Card

```json
{
  "type": "card",
  "title": "Premium Plan",
  "content": "Unlimited API calls, priority support.",
  "tags": ["Recommended", "$99/mo"],
  "image": "https://example.com/premium.png"
}
```

Multiple cards render as a horizontal scrollable group.

### `progress` — Progress Bar

```json
{"type": "progress", "label": "Processing", "value": 75, "max": 100, "status": "running"}
```

`status`: `running` | `success` | `error`.

### `buttons` — Button Group

```json
{
  "type": "buttons",
  "buttons": [
    {"label": "View Details", "value": "details"},
    {"label": "Skip", "value": "skip", "style": "default"}
  ]
}
```

Clicking a button sends the `value` as a new user message — Agent picks it up in the next turn. No blocking needed.

`style`: `primary` (default) | `default` | `danger`.

### `form` — Input Form

```json
{
  "type": "form",
  "fields": [
    {"name": "project", "label": "Project Name", "field_type": "text", "required": true},
    {"name": "lang", "label": "Language", "field_type": "select", "options": ["Python", "Go"]}
  ],
  "submit_label": "Create"
}
```

Submitting sends the form data as a JSON user message. Agent processes it in the next turn.

`field_type`: `text` | `textarea` | `select` | `number` | `date` | `checkbox`.

### `choice` — Selection

```json
{
  "type": "choice",
  "prompt": "Which approach?",
  "options": [
    {"label": "Approach A", "value": "a", "description": "Faster"},
    {"label": "Approach B", "value": "b", "description": "More robust"}
  ],
  "multi_select": false
}
```

User's selection is sent as a user message. Same pattern as buttons/form.

## How It Works

```
Agent calls a2ui(blocks=[...])
  │
  ▼
a2ui() tool function
  ├── Validate blocks
  ├── Convert local image paths to file:// URLs
  └── Return ToolResponse with blocks as structured data
  │
  ▼
@agentscope-ai/chat renders tool call
  ├── Matches tool name "a2ui" in customToolRenderConfig
  └── Delegates to <A2UIRenderer blocks={...} />
  │
  ▼
A2UIRenderer
  ├── TextBlock     → Markdown
  ├── DiffBlock     → Monaco diff / react-diff-viewer
  ├── CodeBlock     → Syntax highlighter
  ├── ImageBlock    → <img>
  ├── TableBlock    → <Table>
  ├── CardBlock     → <Card> horizontal scroll
  ├── ProgressBlock → <Progress>
  ├── ButtonsBlock  → <Button> group → click sends user message
  ├── FormBlock     → <Form> → submit sends user message
  └── ChoiceBlock   → Radio/Checkbox → select sends user message
```

### Interactive Blocks (buttons / form / choice)

No blocking, no Future, no API endpoint. When the user clicks/submits:

1. Frontend calls `chatRef.current.sendMessage(value)` — injects the user's action as a normal chat message
2. Agent receives it in the next turn and continues

This means the Agent can say "pick one" via `a2ui`, then handle the response like any other user message. Self-evolving conversation.

## Implementation

### Backend

**New file: `src/qwenpaw/agents/tools/a2ui.py`**

```python
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse


async def a2ui(
    blocks: list[dict],
    title: str = "",
) -> ToolResponse:
    """Display rich visual content to the user.

    Use this tool to show structured content beyond plain text:
    diffs, code, images, tables, cards, progress bars, buttons, forms, or choices.

    Args:
        blocks: List of UI blocks. Each block has a "type" field and type-specific fields.
            Supported types:
            - {"type": "text", "content": "markdown text"}
            - {"type": "diff", "file": "path", "language": "python", "old_content": "...", "new_content": "..."}
            - {"type": "code", "language": "python", "content": "...", "filename": "example.py"}
            - {"type": "image", "url": "path or URL", "alt": "description"}
            - {"type": "table", "headers": ["A","B"], "rows": [["1","2"]]}
            - {"type": "card", "title": "...", "content": "...", "tags": [...], "image": "url"}
            - {"type": "progress", "label": "...", "value": 75, "max": 100, "status": "running"}
            - {"type": "buttons", "buttons": [{"label": "OK", "value": "ok", "style": "primary"}]}
            - {"type": "form", "fields": [{"name": "x", "label": "X", "field_type": "text"}], "submit_label": "Go"}
            - {"type": "choice", "prompt": "Pick one", "options": [{"label": "A", "value": "a"}], "multi_select": false}

        title: Optional heading displayed above the blocks.
    """
    # Validate
    if not blocks or not isinstance(blocks, list):
        return ToolResponse(
            content=[TextBlock(type="text", text="Error: blocks must be a non-empty list.")],
        )

    for block in blocks:
        if not isinstance(block, dict) or "type" not in block:
            return ToolResponse(
                content=[TextBlock(type="text", text="Error: each block must be a dict with a 'type' field.")],
            )

    # Convert local image paths to file:// URLs
    from .send_file import _path_to_file_url
    import os

    for block in blocks:
        if block.get("type") == "image" and block.get("url"):
            url = block["url"]
            if not url.startswith(("http://", "https://", "file://", "data:")):
                expanded = os.path.expanduser(url)
                if os.path.isfile(expanded):
                    block["url"] = _path_to_file_url(expanded)

    return ToolResponse(
        content=[TextBlock(type="text", text="Displayed to user.")],
    )
```

The tool arguments (`blocks`, `title`) are automatically captured by `@agentscope-ai/chat` as `content[0].data.arguments` — the frontend renderer reads them directly. The `ToolResponse` text is just a confirmation for the Agent.

**Modified: `src/qwenpaw/agents/tools/__init__.py`**

```python
from .a2ui import a2ui
# Add to __all__
```

**Modified: `src/qwenpaw/agents/react_agent.py`**

```python
# Add to tool_functions dict and import
"a2ui": a2ui,
```

### Frontend

**New: `console/src/components/ToolRenderers/A2UIRenderer/`**

```
A2UIRenderer/
  ├── index.tsx          # Main component, dispatches to block renderers
  ├── index.module.less  # Styles
  └── blocks/
      ├── TextBlock.tsx
      ├── DiffBlock.tsx
      ├── CodeBlock.tsx
      ├── ImageBlock.tsx
      ├── TableBlock.tsx
      ├── CardBlock.tsx
      ├── ProgressBlock.tsx
      ├── ButtonsBlock.tsx
      ├── FormBlock.tsx
      └── ChoiceBlock.tsx
```

**Registration (in app startup or PluginContext):**

```typescript
// Register a2ui renderer
pluginSystem.addToolRenderers("builtin", {
  a2ui: A2UIRenderer,
});
```

Or directly in the Chat options:

```typescript
customToolRenderConfig: {
  a2ui: A2UIRenderer,
  ...pluginToolRenderers,
}
```

**A2UIRenderer receives** `{ data: IAgentScopeRuntimeMessage }` where:
- `data.content[0].data.arguments.blocks` — the block list
- `data.content[0].data.arguments.title` — the optional title

**Interactive block click handler:**

```typescript
// When user clicks a button / submits form / selects choice:
const chatRef = useChatAnywhereContext();  // or passed via props
chatRef.sendMessage(JSON.stringify({ a2ui_response: value }));
```

### File Summary

| Action | File |
|--------|------|
| New | `src/qwenpaw/agents/tools/a2ui.py` |
| New | `console/src/components/ToolRenderers/A2UIRenderer/index.tsx` |
| New | `console/src/components/ToolRenderers/A2UIRenderer/index.module.less` |
| New | `console/src/components/ToolRenderers/A2UIRenderer/blocks/*.tsx` (10 files) |
| Edit | `src/qwenpaw/agents/tools/__init__.py` — add export |
| Edit | `src/qwenpaw/agents/react_agent.py` — add to toolkit |
| Edit | `console/src/pages/Chat/index.tsx` — add a2ui to customToolRenderConfig |

## Usage Examples

### After edit_file — show the diff

```python
await edit_file("src/main.py", "def foo():", "def foo(x: int):")
await a2ui(blocks=[
    {"type": "diff", "file": "src/main.py", "language": "python",
     "old_content": "def foo():", "new_content": "def foo(x: int):"},
])
```

### Show an image to the user

```python
await a2ui(blocks=[
    {"type": "image", "url": "/tmp/chart.png", "alt": "Sales Q4"},
])
```

### Present options for user to choose

```python
await a2ui(
    title="How should I proceed?",
    blocks=[
        {"type": "choice", "prompt": "Pick a strategy:",
         "options": [
             {"label": "Quick fix", "value": "quick", "description": "Patch the bug only"},
             {"label": "Full refactor", "value": "refactor", "description": "Rewrite the module"},
         ]},
    ],
)
# Agent continues; if user clicks "Quick fix", it arrives as a user message
```

### Project overview dashboard

```python
await a2ui(
    title="Project Status",
    blocks=[
        {"type": "progress", "label": "Tests passing", "value": 47, "max": 52, "status": "running"},
        {"type": "table", "headers": ["Module", "Coverage", "Status"],
         "rows": [["auth", "92%", "OK"], ["api", "78%", "Warning"], ["db", "45%", "Critical"]]},
        {"type": "buttons", "buttons": [
            {"label": "Run full test suite", "value": "run_tests", "style": "primary"},
            {"label": "Show failures", "value": "show_failures"},
        ]},
    ],
)
```

### Unknown block type fallback

Frontend renders unknown types as collapsed JSON — forward compatible with future block types.
