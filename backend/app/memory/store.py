"""基于内存的会话存储，支持 LRU 淘汰 + token 预算制截断。"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

# 上下文 token 预算（输入历史 + 当前消息的总预算，近似值，无 tokenizer 依赖）
MAX_TOKENS = 8000

# 兜底：消息绝对轮数上限（防止极端短消息导致预算内无限堆积；预算优先）
MAX_TURNS = 200

# 最多同时保活的 session 数
MAX_SESSIONS = 200

# session 空闲多久后过期（秒）—— 默认 4 小时
SESSION_TTL_SECONDS = 4 * 3600


def estimate_tokens(text) -> int:
    """无 tokenizer 的保守近似，兼容多模态 content / 工具 JSON。"""
    if text is None or text == "":
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str, separators=(",", ":"))
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, math.ceil(cjk * 1.5 + (len(text) - cjk) / 4))


def estimate_message_tokens(message) -> int:
    """计入角色封装、工具名/参数/ID，避免空 content 的调用被计作零。"""
    payload = {"role": getattr(message, "type", "unknown"),
               "content": getattr(message, "content", "")}
    for field in ("tool_calls", "invalid_tool_calls", "tool_call_id", "name"):
        value = getattr(message, field, None)
        if value:
            payload[field] = value
    raw_calls = getattr(message, "additional_kwargs", {}).get("tool_calls")
    if raw_calls and not payload.get("tool_calls"):
        payload["tool_calls"] = raw_calls
    return 6 + estimate_tokens(payload)


def estimate_messages_tokens(messages: list) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tool_tokens(tools) -> int:
    """计入真正提供给模型的工具定义；复用 LangChain 已有序列化。"""
    if not tools:
        return 0
    from langchain_core.utils.function_calling import convert_to_openai_tool
    schemas = [tool if isinstance(tool, dict) else convert_to_openai_tool(tool) for tool in tools]
    return estimate_tokens(schemas) + 8


def _tool_call_ids(message) -> list[str]:
    calls = getattr(message, "tool_calls", None) or getattr(message, "additional_kwargs", {}).get("tool_calls", [])
    return [str(call.get("id") or "") for call in calls]


def protocol_groups(messages: list) -> list[list]:
    """工具请求及全部响应为原子组；丢弃孤立响应及未完成的请求组。

    只支持协议要求的紧邻工具响应。失败/中断的历史请求不能重新发给模型。
    """
    groups = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, ToolMessage):
            index += 1
            continue
        ids = _tool_call_ids(message) if isinstance(message, AIMessage) else []
        if not ids:
            groups.append([message])
            index += 1
            continue
        end = index + 1
        responses = []
        while end < len(messages) and isinstance(messages[end], ToolMessage):
            responses.append(str(messages[end].tool_call_id))
            end += 1
        if all(ids) and len(set(ids)) == len(ids) and sorted(responses) == sorted(ids):
            groups.append(messages[index:end])
        index = end
    return groups


def budget_history(messages: list, budget: int) -> list:
    """按真实消息成本裁剪历史，严格服从预算并保护工具调用配对。"""
    if budget <= 0 or not messages:
        return []
    keep = []
    used = 0
    for group in reversed(protocol_groups(messages)):
        cost = estimate_messages_tokens(group)
        if used + cost > budget:
            break
        keep.append(group)
        used += cost
    return [message for group in reversed(keep) for message in group]


class ConversationStore:
    """线程安全的 LRU 会话存储。"""

    def __init__(
        self,
        max_turns: int = MAX_TURNS,
        max_sessions: int = MAX_SESSIONS,
        ttl: int = SESSION_TTL_SECONDS,
        max_tokens: int = MAX_TOKENS,
    ):
        self._max_turns = max_turns
        self._max_sessions = max_sessions
        self._ttl = ttl
        self._max_tokens = max_tokens
        # key: session_id, value: {"messages": [...], "last_access": timestamp}
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    # ── public ──────────────────────────────────────────────────────────────

    def get(self, session_id: str) -> list:
        """返回该 session 的消息列表；不存在则返回空列表。"""
        with self._lock:
            entry = self._data.get(session_id)
            if entry is None:
                return []
            if time.time() - entry["last_access"] > self._ttl:
                del self._data[session_id]
                return []
            self._data.move_to_end(session_id)  # LRU 刷新
            entry["last_access"] = time.time()
            return list(entry["messages"])  # 返回副本

    def save(self, session_id: str, messages: list) -> None:
        """
        追加本轮新消息到 session 历史，超出 max_turns 时从头部裁剪。
        messages 应包含本轮完整的 user→tool→assistant 链。
        """
        with self._lock:
            entry = self._data.get(session_id)
            if entry is None:
                entry = {"messages": [], "last_access": time.time()}
                self._data[session_id] = entry

            # 追加新消息
            entry["messages"].extend(messages)
            entry["last_access"] = time.time()

            # 超出 max_turns 时按 HumanMessage 为边界从头部裁剪
            self._trim(entry)

            # LRU 淘汰过多 session
            self._data.move_to_end(session_id)
            while len(self._data) > self._max_sessions:
                self._data.popitem(last=False)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_sessions": len(self._data),
                "max_sessions": self._max_sessions,
                "max_turns": self._max_turns,
            }

    # ── internal ─────────────────────────────────────────────────────────────

    def _trim(self, entry: dict) -> None:
        """按 token 预算从头部裁剪（保留最近的会话内容）；轮数上限兜底。"""
        msgs = entry["messages"]

        # 协议组为最小裁剪单元，预算为零时不能恢复整段历史。
        entry["messages"] = msgs = budget_history(msgs, self._max_tokens)

        # 2) 轮数兜底：HumanMessage 条数 > max_turns 时仍裁剪（极端短消息场景）
        if self._max_turns <= 0:
            entry["messages"] = []
            return
        human_count = sum(1 for m in msgs if isinstance(m, HumanMessage))
        if human_count <= self._max_turns:
            return
        to_remove = human_count - self._max_turns
        removed = 0
        cut_idx = 0
        for i, m in enumerate(msgs):
            if isinstance(m, HumanMessage):
                removed += 1
                if removed > to_remove:
                    cut_idx = i
                    break
        entry["messages"] = msgs[cut_idx:]
