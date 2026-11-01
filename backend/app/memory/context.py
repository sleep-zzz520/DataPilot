"""统一模型输入预算：必需状态优先，再分配记忆和配对安全的历史。"""
from __future__ import annotations

from dataclasses import dataclass
from langchain_core.messages import HumanMessage, SystemMessage

from app.memory.store import (
    MAX_TOKENS, budget_history, estimate_message_tokens,
    estimate_messages_tokens, estimate_tool_tokens,
)


class ContextBudgetExceeded(ValueError):
    """必需输入和预留输出已超限，调用方应让用户缩小输入或提高预算。"""


@dataclass(frozen=True)
class ContextAssembly:
    messages: list
    history: list
    system_prompt: str
    estimated_tokens: int
    tool_tokens: int
    output_reserve: int
    dropped_messages: int


def assemble_context(system_prompt: str, history: list, user_message, *,
                     memory_context: str | None = None,
                     task_context: str | None = None, tools=(),
                     max_tokens: int = MAX_TOKENS,
                     output_reserve: int = 1024) -> ContextAssembly:
    """同一预算覆盖 system、当前问题、工具定义、历史、记忆及输出预留。

    task_context 是当前任务的权威状态，不可截断；memory_context 是有来源的
    候选历史，只注入能完整容纳的段落，最多占剩余输入预算的一半。
    ponytail: token 是无供应商依赖的估算，生产可用模型 tokenizer 替换估算器。
    """
    if max_tokens <= 0 or output_reserve < 0:
        raise ContextBudgetExceeded("上下文预算必须为正，输出预留不能为负")
    user = user_message if isinstance(user_message, HumanMessage) else HumanMessage(content=user_message)
    prompt = system_prompt + ("\n\n" + task_context if task_context else "")
    tool_tokens = estimate_tool_tokens(tools)
    available = max_tokens - output_reserve - tool_tokens
    required = estimate_messages_tokens([SystemMessage(content=prompt), user])
    if required > available:
        raise ContextBudgetExceeded("系统规则、当前任务、用户输入与工具定义超过上下文预算")
    remaining = available - required
    if memory_context:
        # 每段为原子块，优先保留同块来源，且保留记忆的证据优先级规则。
        guard = "历史记忆仅作参考；当前任务、当前数据和当前证据优先。"
        blocks = []
        for block in memory_context.split("\n\n"):
            candidate = prompt + "\n\n" + guard + "\n" + "\n\n".join(blocks + [block])
            extra = estimate_message_tokens(SystemMessage(content=candidate)) - estimate_message_tokens(SystemMessage(content=prompt))
            if extra <= remaining // 2:
                blocks.append(block)
        if blocks:
            prompt += "\n\n" + guard + "\n" + "\n\n".join(blocks)
    used = estimate_messages_tokens([SystemMessage(content=prompt), user])
    kept = budget_history(history, available - used)
    messages = [SystemMessage(content=prompt)] + kept + [user]
    return ContextAssembly(messages, kept, prompt,
                           estimate_messages_tokens(messages) + tool_tokens + output_reserve,
                           tool_tokens, output_reserve, len(history) - len(kept))


def bound_context_messages(system_prompt: str, messages: list, *, tools=(),
                           max_tokens: int = MAX_TOKENS,
                           output_reserve: int = 1024) -> ContextAssembly:
    """执行循环专用：保持当前 user→工具调用链顺序，历史才允许裁剪。"""
    if max_tokens <= 0 or output_reserve < 0:
        raise ContextBudgetExceeded("上下文预算必须为正，输出预留不能为负")
    # SystemMessage 由调用者单独传入，防止每次循环重复累加。
    body = [message for message in messages if not isinstance(message, SystemMessage)]
    start = max((i for i, message in enumerate(body) if isinstance(message, HumanMessage)), default=0)
    current = body[start:]
    tool_tokens = estimate_tool_tokens(tools)
    system = SystemMessage(content=system_prompt)
    required = estimate_messages_tokens([system] + current) + tool_tokens + output_reserve
    if required > max_tokens:
        raise ContextBudgetExceeded("当前工具轮、系统规则和工具定义超过上下文预算")
    kept = budget_history(body[:start], max_tokens - required)
    result = [system] + kept + current
    return ContextAssembly(result, kept, system_prompt,
                           estimate_messages_tokens(result) + tool_tokens + output_reserve,
                           tool_tokens, output_reserve, start - len(kept))
