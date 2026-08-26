# Trick API Reference

## Trick base class (`petsitter.trick.Trick`)

```python
class Trick:
    __brief__: str = ""
    __display_name__: str = ""
    keywords: list[str] = []
    required_models: list[str] = ["default"]

    def system_prompt(self, to_add: str) -> str: ...
    def pre_hook(self, context: list, params: dict) -> list: ...
    def post_hook(self, context: list) -> list: ...
    def info(self, capabilities: dict) -> dict: ...
```

All hooks default to returning their input unchanged. Override only the hooks you need.

### Class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `__brief__` | `str` | One-line summary shown in the dashboard GUI. Every trick should set this. |
| `__display_name__` | `str` | Human-readable name for the GUI. Falls back to the class name if empty. |
| `keywords` | `list[str]` | If set, the trick only activates when at least one keyword appears in the user's message. Keywords are stripped from the message before sending to the model. |
| `required_models` | `list[str]` | Model keys this trick needs from a modelset. Default is `["default"]`. Multi-model tricks override with additional keys like `["default", "thinker", "toolcall"]`. |

### `system_prompt(to_add: str) -> str`

Called once per request, before anything is sent to the upstream model.

- `to_add`: The current system prompt content (or `""` if none).
- Return: Modified system prompt. Return `""` to leave unchanged.
- Multiple tricks each get a chance to modify the system prompt in order.
- Use this to inject formatting rules, tool calling instructions, or behavior constraints.

### `pre_hook(context: list, params: dict) -> list`

Called after the system prompt is finalized but before the request is sent to the model.

- `context`: List of message dicts `[{"role": str, "content": str}, ...]`. The system prompt is the first message if present.
- `params`: The full request parameters dict. Contains `tools`, `temperature`, `max_tokens`, etc.
- Return: Modified context list.
- Use this to inject tool definitions into `params["tools"]`, modify messages, or add additional context.

### `post_hook(context: list) -> list`

Called after the upstream model responds, with the assistant's response appended to the context.

- `context`: Messages list with the model's response as the last entry: `context[-1]` is `{"role": "assistant", "content": "...", ...}`.
- Return: Modified context. The last message becomes the final response.
- Use this to validate output (e.g. JSON parsing), retry with feedback via `callmodel`, detect and reformat tool calls, or transform the response content.

Note that `post_hook` is not given `params`. Anything it needs to know about the request that produced the response comes from the request metadata channel below — **not** from state cached on `self`.

## Request metadata (`petsitter.observability`)

### `request_meta() -> dict`

Per-request metadata, carried alongside the payload for the life of one request. Backed by a `contextvar`, so concurrent requests each get their own and cannot see each other's.

The proxy populates it before any hook runs:

| Key | Value |
|---|---|
| `request_id` | Short correlation id, the same one `request_tag()` prints |
| `payload` | The full incoming request body |
| `tools` | `payload["tools"]`, or `[]` |
| `model` | The requested model string |
| `stream` | Whether the client asked for a stream |

Tricks may also write to it as scratch space to carry their own state from one hook to another within a single request:

```python
def pre_hook(self, context, params):
    request_meta()["my_trick_saw_tools"] = bool(params.get("tools"))
    return context

def post_hook(self, context):
    if not request_meta().get("my_trick_saw_tools"):
        return context
    ...
```

**Do not use instance attributes for per-request state.** A trick object is shared across every concurrent request in its trickset, so `self._something = ...` in `pre_hook` can be overwritten by another request before `post_hook` reads it. Instance attributes are for configuration and for state that is deliberately long-lived (caches, counters, tallies).

Outside a request — a lifecycle hook, or a direct call in a test — `request_meta()` returns an inert empty dict, so reads are safe and writes are discarded. Tests that exercise `pre_hook`/`post_hook` together should open one explicitly with `start_request_meta()` / `reset_request_meta(token)`.

### `info(capabilities: dict) -> dict`

Called when building the final response to declare capabilities.

- `capabilities`: Accumulated dict from earlier tricks' `info()` calls.
- Return: Updated capabilities dict. Add keys but don't remove existing ones.
- Example: `capabilities["json_mode"] = True`

## Context utilities (`petsitter.context`)

```python
from petsitter.context import (
    get_system_prompt,
    set_system_prompt,
    append_to_system_prompt,
    get_last_message,
    set_last_message_content,
    add_message,
)
```

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_system_prompt` | `(context) -> str` | Extract system prompt content from first message |
| `set_system_prompt` | `(context, content) -> list` | Set or replace the system prompt |
| `append_to_system_prompt` | `(context, addition) -> list` | Append text to the system prompt |
| `get_last_message` | `(context) -> dict\|None` | Get the last message in context |
| `set_last_message_content` | `(context, content) -> list` | Replace the last message's content |
| `add_message` | `(context, role, content) -> list` | Append a new message |

## `callmodel` utilities (`petsitter.trick`)

Two helpers for making follow-up calls to the upstream model from within a trick.

### `callmodel_sync(context, user_message="") -> list`

Synchronous. Uses the globally configured model URL (set during ProxyHandler init).

- Appends `user_message` as a user message, calls the model, returns the updated context with the assistant's response appended.
- Best for simple retry loops from `post_hook`.

### `callmodel(context, instruction="", model_url="", model_name="", api_key="") -> list`

Async. Requires `model_url`.

- Appends `instruction` to the system prompt (or creates one), calls the model, returns updated context.
- Use when you need to pass a custom model URL or need async operation.

## Message format

Each message is a dict:

```python
{"role": "system" | "user" | "assistant" | "tool", "content": str}
```

For tool calls, the assistant message may also contain:

```python
{
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "tool_name", "arguments": '{"key": "val"}'}
        }
    ]
}
```

## File structure

```
tricks/
├── __init__.py
├── your_trick.py      # <-- your trick goes here
├── code_validator.py   # self-healing code validation via model self-description
├── tool_call.py        # built-in examples
├── json_mode.py        # JSON output enforcement
├── xml_tool.py         # XML-style tool calling
└── ...
```

The file must define exactly one class that subclasses `Trick`. Helper functions and additional classes are fine as long as they don't subclass `Trick`.
