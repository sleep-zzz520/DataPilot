"""Semantic repair traverses production graphs, DuckDB and controlled evidence tools.

Only LLM output and owner configuration are faked. SQL queries, semantic gates,
local repair edges, evidence consumers and approval storage execute real code.
"""
import json
import re

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.analysis_plan import AnalysisPlan, PlanRuntime
from app.agent.contracts import ContractBinding, MetricContract, MetricContractRegistry, bind_metric_contracts
from app.agent.multi_agent import make_agent
from app.api import chat_api as api
from app.persistence import save_pending_approval
from app.tools import file_tool
from app.tools.agent_tools import make_tools
from app.tools.evidence_tool import make_evidence_tools


VALID_SQL = ("SELECT region, SUM(sales_amount)-SUM(refund_amount) AS net_sales "
             "FROM df WHERE month = '2026-01' GROUP BY region ORDER BY region")
BAD_SQL = VALID_SQL.replace("month = '2026-01'", "1=1")


def call(name, args, cid):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


class ScriptedLLM(BaseChatModel):
    responses: list
    _index: int = 0

    @property
    def _llm_type(self):
        return "semantic-integration-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self, messages):
        response = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return response(messages) if callable(response) else response

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        value = self._next(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=value.content, tool_calls=value.tool_calls))


@pytest.fixture
def data():
    return pd.DataFrame({
        "month": ["2026-01", "2026-01", "2026-02"],
        "region": ["East", "West", "East"],
        "sales_amount": [100.0, 200.0, 900.0],
        "refund_amount": [10.0, 20.0, 0.0],
        "cost_amount": [60.0, 120.0, 800.0],
    })


@pytest.fixture
def contract():
    return MetricContract(
        id="file.net_sales.jan", version="1", name="一月净销售额",
        formula="SUM(sales_amount)-SUM(refund_amount)",
        required_columns=("sales_amount", "refund_amount", "region", "month"),
        required_filters=("month = '2026-01'",), group_by=("region",), grain="region",
        source="df", unit="CNY", scope="approved January booked sales less refunds",
        metric_names=("净销售额",),
    )


def runtime_for(contract, data, *, calculations=False):
    steps = [{"id": "q", "title": "按指标定义取数", "kind": "query"},
             {"id": "v", "title": "证据验证", "kind": "validate", "depends_on": ["q"]}]
    if calculations:
        steps += [{"id": "calculate", "title": "比较地区", "kind": "calculate", "depends_on": ["q"]},
                  {"id": "chart", "title": "绘制地区图", "kind": "visualize", "depends_on": ["calculate"]}]
    plan = AnalysisPlan(goal="一月净销售额", metrics=["净销售额"], steps=steps)
    catalog = [{"name": "df", "columns": list(data.columns), "file_name": "sales.csv"}]
    binding = bind_metric_contracts("一月净销售额", plan, catalog,
        MetricContractRegistry([contract], [contract.id]), contract_ids=[contract.id])
    return PlanRuntime(plan, "一月净销售额", binding=binding, catalog=catalog)


@pytest.fixture
def executed_sql(monkeypatch):
    executed = []
    original = file_tool._query_dataframe

    def capture(df, sql):
        executed.append(sql)
        return original(df, sql)

    monkeypatch.setattr(file_tool, "_query_dataframe", capture)
    return executed


def query_call(sql, cid):
    return call("query_file", {"file": "sales.csv", "sql": sql}, cid)


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_wrong_filter_repairs_through_graph_and_only_correct_query_reaches_duckdb(
        contract, data, executed_sql, mode):
    runtime = runtime_for(contract, data)
    responses = [query_call(BAD_SQL, "wrong"), query_call(VALID_SQL, "fixed"),
                 AIMessage(content="一月East为90元，West为180元。")]
    if mode == "multi":
        responses = [call("data_executor", {"request": "查询一月净销售额"}, "worker")] + responses + [
            call("statistical_validator", {"request": "核对一月口径与实际证据"}, "validator"),
            AIMessage(content="口径和真实证据一致。"),
            AIMessage(content="一月East为90元，West为180元。"),
        ]
    llm = ScriptedLLM(responses=responses)
    graph, _ = make_agent(llm, make_tools(None, files={"sales.csv": data}),
                          plan_runtime=runtime, mode=mode, question="一月净销售额")
    result = graph.invoke({"messages": [HumanMessage(content="一月净销售额")]})
    assert executed_sql == [VALID_SQL + " LIMIT 500"]
    assert runtime.validate_evidence()["status"] == "approved"
    assert len(runtime.plan.repair_history) == 1
    repair = runtime.plan.repair_history[0]
    assert repair["before"]["sql"] == BAD_SQL
    assert repair["after"]["sql"] == VALID_SQL
    assert repair["changed_fields"]["sql"] == {"before": BAD_SQL, "after": VALID_SQL}
    evidence_id = runtime.plan.steps[0].evidence_ids[0]
    assert runtime.evidence_rows([evidence_id])["rows"] == [["East", 90.0], ["West", 180.0]]
    assert result["messages"][-1].content.endswith("West为180元。")
    assert graph.route_decision["selected_mode"] == mode


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_repeated_semantic_error_stops_after_two_repairs_without_query_execution(
        contract, data, executed_sql, mode):
    runtime = runtime_for(contract, data)
    responses = [query_call(BAD_SQL, f"wrong{attempt}") for attempt in range(10)]
    if mode == "multi":
        responses.insert(0, call("data_executor", {"request": "查询净销售额"}, "worker"))
    llm = ScriptedLLM(responses=responses)
    graph, _ = make_agent(llm, make_tools(None, files={"sales.csv": data}),
                          plan_runtime=runtime, mode=mode, question="一月净销售额")
    result = graph.invoke({"messages": [HumanMessage(content="一月净销售额")]})
    assert executed_sql == []
    assert len(runtime.plan.repair_history) == 2
    assert runtime.plan.steps[0].retry_count == 2
    assert runtime.validate_evidence()["status"] == "rejected"
    assert not runtime.should_retry()
    assert "停止" in result["messages"][-1].content
    assert llm._index <= (4 if mode == "multi" else 3)


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_missing_profit_cost_field_clarifies_before_any_llm_or_query(
        contract, data, executed_sql, mode):
    profit = contract.model_copy(update={
        "id": "file.net_profit.jan", "name": "一月净利润", "metric_names": ("净利润",),
        "formula": "SUM(sales_amount)-SUM(refund_amount)-SUM(cost_amount)",
        "required_columns": contract.required_columns + ("cost_amount",),
    })
    runtime = runtime_for(profit, data.drop(columns=["cost_amount"]))
    assert runtime.next_action() == "clarify"
    llm = ScriptedLLM(responses=[query_call(VALID_SQL, "forbidden")])
    graph, _ = make_agent(llm, make_tools(None, files={"sales.csv": data}),
                          plan_runtime=runtime, mode=mode, question="一月净利润")
    result = graph.invoke({"messages": [HumanMessage(content="一月净利润")]})
    assert llm._index == 0
    assert executed_sql == []
    assert runtime.validate_evidence()["status"] == "rejected"
    assert "必要字段" in result["messages"][-1].content


def test_statistics_and_chart_use_real_evidence_and_reject_invalidated_reference(
        contract, data, executed_sql):
    runtime = runtime_for(contract, data, calculations=True)
    tools = make_tools(None, files={"sales.csv": data})

    def calculate(_messages):
        return call("calculate_from_evidence", {
            "evidence_id": runtime.plan.steps[0].evidence_ids[0], "operation": "compare_groups",
            "parameters": {"group_by": ["region"], "value_col": "net_sales"},
        }, "calculate")

    def chart(_messages):
        return call("chart_from_evidence", {
            "evidence_id": runtime.plan.steps[0].evidence_ids[0], "chart_type": "bar",
            "x_column": "region", "y_columns": ["net_sales"], "title": "一月净销售额",
        }, "chart")

    llm = ScriptedLLM(responses=[query_call(VALID_SQL, "q"), calculate, chart,
                                AIMessage(content="East为90元，West为180元。")])
    graph, _ = make_agent(llm, tools, plan_runtime=runtime, mode="single", question="一月净销售额")
    result = graph.invoke({"messages": [HumanMessage(content="计算并绘制一月净销售额")]})
    assert executed_sql == [VALID_SQL + " LIMIT 500"]
    assert runtime.validation_status == "approved"
    evidence_id = runtime.plan.steps[0].evidence_ids[0]
    calculations = next(m.content for m in result["messages"]
                        if isinstance(m, ToolMessage) and m.name == "calculate_from_evidence")
    envelope = json.loads(calculations.split("\n【受控证据引用】", 1)[0])
    assert envelope["evidence_ids"] == [evidence_id]
    statistical_result = json.loads(envelope["result"])
    assert statistical_result["status"] == "ok"
    chart_message = next(m.content for m in result["messages"]
                         if isinstance(m, ToolMessage) and m.name == "chart_from_evidence")
    chart_payload = json.loads(re.search(r"<!--CHART:(.*?)-->", chart_message, re.S).group(1))
    assert chart_payload["series"][0]["data"] == [90.0, 180.0]
    assert runtime.plan.steps[2].output["evidence_ids"] == [evidence_id]
    safe_tools = {item.name: item for item in make_evidence_tools(runtime, tools)}
    runtime.invalidate_step("q")
    rejected_stats = json.loads(safe_tools["calculate_from_evidence"].invoke({
        "evidence_id": evidence_id, "operation": "compare_groups",
        "parameters": {"group_by": ["region"], "value_col": "net_sales"},
    }))
    rejected_chart = json.loads(safe_tools["chart_from_evidence"].invoke({
        "evidence_id": evidence_id, "chart_type": "bar", "x_column": "region",
        "y_columns": ["net_sales"], "title": "过期证据不得出图",
    }))
    assert rejected_stats["status"] == rejected_chart["status"] == "error"
    assert "失效" in rejected_stats["message"]


def request_for(*, contract_ids=None, **kwargs):
    return api.ChatRequest(message="一月净销售额", session_id="semantic-session", llm_config_id=1,
                           db_config_id=42, file_ids=["upload1"], metric_contract_ids=contract_ids or [], **kwargs)


@pytest.fixture
def owner_config(tmp_path, monkeypatch, isolated_storage):
    path = tmp_path / "metric_contracts.json"
    monkeypatch.setattr(api, "_CONTRACT_CONFIG", path)
    monkeypatch.setattr(api, "get_upload", lambda fid, uid: {
        "name": "sales.csv", "columns": ["sales_amount", "refund_amount", "month", "region"],
    } if fid == "upload1" and uid == 7 else None)
    monkeypatch.setattr(api, "_audit_hitl", lambda *args, **kwargs: None)
    return path


def write_config(path, contract, *, allowed_files=("sales.csv",)):
    path.write_text(json.dumps({"contracts": [contract.model_dump(mode="json")],
        "enabled_contract_ids": [contract.id],
        "source_bindings": {contract.id: {"file_names": list(allowed_files)}},
    }))


def test_empty_contract_configuration_preserves_legacy_request(owner_config, monkeypatch):
    owner_config.write_text("{}")
    plan = AnalysisPlan(goal="旧分析", metrics=["未知业务指标"])
    monkeypatch.setattr(api, "load_contract_catalog", lambda *args: pytest.fail("legacy request must not inspect schemas"))
    binding, catalog = api._bind_request_contracts(request_for(), plan, object(), {"uid": 7})
    assert binding is None and catalog == []
    assert not plan.clarification_needed and not plan.bound_contracts


@pytest.mark.parametrize("uid,allowed_files", [(7, ("other.csv",)), (8, ("sales.csv",))])
def test_enabled_contract_rejects_unconfigured_file_or_other_user(
        owner_config, contract, uid, allowed_files):
    write_config(owner_config, contract, allowed_files=allowed_files)
    plan = AnalysisPlan(goal="一月净销售额", metrics=["净销售额"])
    binding, _catalog = api._bind_request_contracts(
        request_for(contract_ids=[contract.id]), plan, object(), {"uid": uid})
    assert any(issue.code == "CONTRACT_SOURCE_BINDING_REQUIRED" for issue in binding.issues)
    assert plan.clarification_needed


def test_enabled_contract_ids_bind_only_owner_registered_definition(owner_config, contract):
    write_config(owner_config, contract)
    plan = AnalysisPlan(goal="一月净销售额", metrics=["净销售额"])
    binding, catalog = api._bind_request_contracts(
        request_for(contract_ids=[contract.id]), plan, object(), {"uid": 7})
    assert not binding.issues
    assert binding.contracts == (contract,)
    assert catalog[0]["file_name"] == "sales.csv"
    assert plan.bound_contracts[0]["formula"] == contract.formula


@pytest.mark.parametrize("change", ["version", "formula", "contract_ids"])
def test_approval_cannot_replay_after_contract_definition_or_selection_changes(
        owner_config, contract, change):
    write_config(owner_config, contract)
    req = request_for(contract_ids=[contract.id])
    plan = AnalysisPlan(goal="一月净销售额", metrics=["净销售额"])
    save_pending_approval("approval1", 7, req.session_id, api._request_hash(req, req.session_id),
                          {"plan": plan.model_dump(), "confirmation": {"required": True}})
    if change == "version":
        write_config(owner_config, contract.model_copy(update={"version": "2"}))
    elif change == "formula":
        write_config(owner_config, contract.model_copy(update={"formula": "SUM(sales_amount)"}))
    resumed = request_for(contract_ids=["other.metric"] if change == "contract_ids" else [contract.id],
                          approval_id="approval1", approval_decision="approve")
    with pytest.raises(ValueError, match="原始分析不匹配"):
        api._resolve_plan(resumed, resumed.session_id, None, object(), None, {"uid": 7})


def test_unchanged_contract_approval_resumes_once_and_replay_is_rejected(owner_config, contract):
    write_config(owner_config, contract)
    req = request_for(contract_ids=[contract.id])
    plan = AnalysisPlan(goal="一月净销售额", metrics=["净销售额"], steps=[
        {"id": "q", "title": "查询", "kind": "query"}])
    save_pending_approval("approved-once", 7, req.session_id, api._request_hash(req, req.session_id),
                          {"plan": plan.model_dump(), "confirmation": {"required": True}})
    resumed = request_for(contract_ids=[contract.id], approval_id="approved-once", approval_decision="approve")
    resolution = api._resolve_plan(resumed, resumed.session_id, None, object(), None, {"uid": 7})
    assert resolution["kind"] == "ready"
    assert resolution["binding"].contracts == (contract,)
    assert resolution["plan"].bound_contracts[0]["version"] == contract.version
    with pytest.raises(ValueError, match="已处理"):
        api._resolve_plan(resumed, resumed.session_id, None, object(), None, {"uid": 7})


def test_database_contract_cannot_switch_to_different_config_id(owner_config, contract, monkeypatch):
    database_contract = contract.model_copy(update={"source": "sales"})
    owner_config.write_text(json.dumps({
        "contracts": [database_contract.model_dump(mode="json")],
        "enabled_contract_ids": [contract.id],
        "source_bindings": {contract.id: {"db_config_ids": [99]}},
    }))
    monkeypatch.setattr(api, "load_contract_catalog", lambda engine, sources: [
        {"name": "sales", "columns": list(contract.required_columns)}])
    req = request_for(contract_ids=[contract.id]).model_copy(update={"file_ids": []})
    binding, catalog = api._bind_request_contracts(
        req, AnalysisPlan(goal="净销售额", metrics=["净销售额"]), object(), {"uid": 7})
    assert catalog[0]["name"] == "sales"
    assert any(item.code == "CONTRACT_SOURCE_BINDING_REQUIRED" for item in binding.issues)
    approved = req.model_copy(update={"db_config_id": 99})
    allowed, _ = api._bind_request_contracts(
        approved, AnalysisPlan(goal="净销售额", metrics=["净销售额"]), object(), {"uid": 7})
    assert not allowed.issues


@pytest.mark.parametrize("mode", ["single", "multi"])
@pytest.mark.parametrize("raw_has_reference", [True, False])
def test_raw_chart_input_is_blocked_then_safe_chart_repairs_from_evidence(contract, data, executed_sql, mode, raw_has_reference):
    runtime = runtime_for(contract, data)
    from app.agent.analysis_plan import PlanStep
    runtime.plan.steps.append(PlanStep(id="chart", title="绘制图表", kind="visualize", depends_on=["q"]))

    def forged_chart(_messages):
        return call("make_chart", {
            **({"evidence_id": runtime.plan.steps[0].evidence_ids[0]} if raw_has_reference else {}),
            "chart_type": "bar", "title": "伪造", "x_labels": ["East", "West"],
            "series": [{"name": "net_sales", "data": [999999, 999999]}],
        }, "forged")

    def safe_chart(_messages):
        return call("chart_from_evidence", {
            "evidence_id": runtime.plan.steps[0].evidence_ids[0],
            "chart_type": "bar", "title": "可信图", "x_column": "region", "y_columns": ["net_sales"],
        }, "safe")

    responses = [query_call(VALID_SQL, "q"), forged_chart, safe_chart,
                 AIMessage(content="图表使用真实一月净销售额。")]
    if mode == "multi":
        responses = [call("data_executor", {"request": "取得一月净销售额"}, "data"),
            query_call(VALID_SQL, "q"), AIMessage(content="已取得真实证据。"),
            call("statistical_validator", {"request": "核对实际证据"}, "validate"),
            AIMessage(content="实际证据通过。"),
            call("insight_writer", {"request": "绘制经过验证的地区净销售额"}, "insight"),
            forged_chart, safe_chart, AIMessage(content="已绘图。"),
            AIMessage(content="图表使用真实一月净销售额。")]
    llm = ScriptedLLM(responses=responses)
    graph, _ = make_agent(llm, make_tools(None, files={"sales.csv": data}),
                          plan_runtime=runtime, mode=mode, question="一月净销售额")
    result = graph.invoke({"messages": [HumanMessage(content="画图")]})
    if mode == "single":
        blocked = next(message.content for message in result["messages"]
                       if isinstance(message, ToolMessage) and message.name == "make_chart")
        assert "RAW_RECORDS_FORBIDDEN" in blocked and "<!--CHART:" not in blocked
    charts = [json.loads(payload) for message in result["messages"]
              for payload in re.findall(r"<!--CHART:(.*?)-->", str(message.content), re.S)]
    assert len(charts) == 1 and charts[0]["series"][0]["data"] == [90.0, 180.0]
    assert runtime.validate_evidence()["status"] == "approved"
    assert len(runtime.plan.repair_history) == 1
