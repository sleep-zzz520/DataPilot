"""成本治理的可复现离线基准。

这里不调用真实 Provider，也不把固定脚本的结果包装成线上节省金额。基准直接运行
当前 LangGraph：模拟一个在没有停止信号时会连续发起 4 轮无效查询的 Agent，用于
量化 ``MAX_SQL_ATTEMPTS=3`` 实际阻止了多少图内调用。
"""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from app.agent.graph import MAX_SQL_ATTEMPTS, make_graph


class QueryLoopLLM(BaseChatModel):
    """确定性循环模型：无停止信号时查询 4 次，收到停止信号后立刻回答。"""

    calls: int = 0
    planned_queries: int = 4

    @property
    def _llm_type(self) -> str:
        return "cost-governance-benchmark"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next_message(self, messages):
        self.calls += 1
        if any(isinstance(message, SystemMessage) and "停止调用任何查询工具" in message.content
               for message in messages):
            return AIMessageChunk(content="已停止查询循环。")

        query_count = sum(
            isinstance(message, ToolMessage) and message.name == "query_mysql"
            for message in messages
        )
        if query_count >= self.planned_queries:
            return AIMessageChunk(content="查询完成。")
        return AIMessageChunk(
            content="",
            tool_calls=[{
                "name": "query_mysql",
                "args": {"sql": f"SELECT {query_count + 1}"},
                "id": f"query-{query_count + 1}",
            }],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=self._next_message(messages))


def _run_query_loop(max_sql_attempts: int) -> dict:
    executed_sql: list[str] = []

    @tool
    def query_mysql(sql: str) -> str:
        """基准查询工具：记录每一次实际执行。"""
        executed_sql.append(sql)
        return "SQL 执行错误：基准用无效查询"

    llm = QueryLoopLLM()
    result = make_graph(llm, [query_mysql], max_sql_attempts=max_sql_attempts).invoke({
        "messages": [HumanMessage(content="执行查询")],
    })
    return {
        "llm_calls": llm.calls,
        "query_calls": len(executed_sql),
        "sql_attempts": result["sql_attempts"],
    }


def test_three_round_query_guard_quantifies_prevented_loop_calls():
    """固定 4 轮查询循环中，3 轮上限少执行 1 次查询和 1 次 Agent 决策。"""
    baseline = _run_query_loop(max_sql_attempts=4)
    guarded = _run_query_loop(max_sql_attempts=MAX_SQL_ATTEMPTS)

    assert baseline == {"llm_calls": 5, "query_calls": 4, "sql_attempts": 4}
    assert guarded == {"llm_calls": 4, "query_calls": 3, "sql_attempts": 3}

    query_reduction = (baseline["query_calls"] - guarded["query_calls"]) / baseline["query_calls"]
    llm_reduction = (baseline["llm_calls"] - guarded["llm_calls"]) / baseline["llm_calls"]
    assert query_reduction == 0.25
    assert llm_reduction == 0.20
