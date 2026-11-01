"""Select bounded native messages for routing without changing stored history.

The workflow calls this once per turn. Budgets count serialized characters, not
provider tokens. A bounded history leaves capacity for evidence, instructions
and output; it is not an exact provider context-window measurement.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def message_size(message):
    """Count the full native record, including tool arguments and metadata."""
    return len(json.dumps(message.model_dump(), ensure_ascii=False, default=str))


def select_history(messages, current, budget):
    """Return a recent suffix plus the current message within a character limit.

    Human messages delimit atomic turns. Keeping whole turns also keeps parallel
    tool calls and their results together. Incomplete tool exchanges are excluded.
    The current question is mandatory: reject oversize input instead of truncating
    it or silently exceeding the budget. Stored messages are never modified.
    """
    remaining = budget - message_size(current)
    if remaining < 0:
        raise ValueError("Current question exceeds the history budget")
    turns = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turns.append([])
        if turns:
            turns[-1].append(message)
    selected = []
    for turn in reversed(turns):
        calls = {call["id"] for message in turn if isinstance(message, AIMessage)
                 for call in message.tool_calls}
        results = {message.tool_call_id for message in turn if isinstance(message, ToolMessage)}
        size = sum(message_size(message) for message in turn)
        if calls != results or size > remaining:
            break
        selected = turn + selected
        remaining -= size
    return [*selected, current]
