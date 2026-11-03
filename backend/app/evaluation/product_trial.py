"""Isolated worker for quality_runner. Calls the real synchronous chat endpoint.

Only storage, model configuration and the unavailable business-DB boundary are
replaced. Planning, approvals, tools, graphs, memory and final display gates are
production implementations. No plans, SQL proposals or answers are injected.
"""
from __future__ import annotations
import asyncio
import json
import hashlib
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid


class CallBudgetExceeded(RuntimeError):
    pass


class BudgetedModel:
    """Share an attempted-call cap across planner, workers and memory calls."""
    def __init__(self, model, state):
        self.model, self.state = model, state
        self.max_tokens = getattr(model, "max_tokens", None)

    def model_copy(self, update):
        return BudgetedModel(self.model.model_copy(update=update), self.state)

    def bind_tools(self, tools, **kwargs):
        return BudgetedModel(self.model.bind_tools(tools, **kwargs), self.state)

    def _start(self):
        with self.state["lock"]:
            if (self.state["attempted_calls"] >= self.state["max_calls"] or
                    self.state["usage"].input_tokens + self.state["usage"].output_tokens >= self.state.get("max_tokens", float("inf"))):
                self.state["exhausted"] = True
                raise CallBudgetExceeded("evaluation model-call budget exhausted")
            self.state["attempted_calls"] += 1
            self._checkpoint()

    def _checkpoint(self):
        usage = self.state["usage"]
        payload = {"attempted_calls": self.state["attempted_calls"],
                   "completed_calls": usage.calls, "input_tokens": usage.input_tokens,
                   "output_tokens": usage.output_tokens}
        temporary = self.state["progress"].with_suffix(".partial")
        temporary.write_text(json.dumps(payload))
        temporary.replace(self.state["progress"])

    def invoke(self, *args, **kwargs):
        self._start()
        try:
            return self.model.invoke(*args, **kwargs)
        finally:
            with self.state["lock"]:
                self._checkpoint()

    def stream(self, *args, **kwargs):
        self._start()
        try:
            yield from self.model.stream(*args, **kwargs)
        finally:
            with self.state["lock"]:
                self._checkpoint()


def chart_observation(chart):
    """Read the data sent to the actual display, never infer it from prose."""
    if not isinstance(chart, dict):
        return None
    series = chart.get("series", [])
    if not isinstance(series, list) or not series or any(not isinstance(s, dict) for s in series):
        return None
    first = series[0]
    if any(s.get("type") != first.get("type") for s in series):
        return None
    if first.get("type") == "pie":
        data = first.get("data", [])
        if len(series) != 1 or not isinstance(data, list) or any(not isinstance(v, dict) for v in data):
            return None
        return {"type": "pie", "data": {"columns": ["name", "value"],
                "rows": [[v.get("name"), v.get("value")] for v in data if isinstance(v, dict)]}}
    axis = chart.get("xAxis", {})
    if isinstance(axis, list):
        axis = axis[0] if axis else {}
    if not isinstance(axis, dict):
        return None
    labels = axis.get("data", [])
    if not isinstance(labels, list) or not labels:
        return None
    if any(not isinstance(s.get("data"), list) or len(s["data"]) != len(labels) for s in series):
        return None
    rows = []
    for i, label in enumerate(labels):
        values = []
        for s in series:
            data = s.get("data", [])
            value = data[i]
            values.append(value.get("value") if isinstance(value, dict) else value)
        rows.append([label] + values)
    return {"type": first.get("type"), "data": {
        "columns": ["dimension"] + [s.get("name") or "value" for s in series], "rows": rows}}


def infer_behavior(response):
    confirmation = response.get("confirmation") or {}
    if confirmation.get("status") == "rejected":
        return "refuse"
    if confirmation.get("status") not in {"approved", "rejected"} and (confirmation.get("required") or confirmation.get("mode")):
        return "clarify" if confirmation.get("mode") == "clarification" else "approval"
    reply = response.get("reply") or ""
    if not isinstance(reply, str) or not reply.strip() or response.get("error"):
        return "unknown"
    if response.get("tables"):
        return "answer"
    if re.search(r"无法|不能|不支持|无权|未通过|未能|拒绝|不提供|缺少|没有.*数据|不存在", reply):
        return "refuse"
    if re.search(r"请.*(?:提供|补充|确认|明确)|[？?]", reply):
        return "clarify"
    return "unknown"


def execute(payload, root):
    # Must precede importing crypto/auth: no accidental bootstrap in backend/data.
    from cryptography.fernet import Fernet
    os.environ["AGENT_MASTER_KEY"] = Fernet.generate_key().decode()
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    from unittest.mock import patch
    from contextlib import ExitStack
    import pandas as pd
    from langchain_core.tools import StructuredTool
    from app import persistence, auth
    from app.api import chat_api as api
    from app.meta import store
    from app.memory.agent_memory import get_user_memories
    from app.core.factories import build_llm
    from app.usage.callback import RequestUsage
    from app.evaluation.task_suite import materialize_fixture

    task, config = payload["task"], payload["config"]
    progress = root / "progress.json"
    usage = RequestUsage()
    state = {"attempted_calls": 0, "max_calls": payload["max_calls"],
             "usage": usage, "progress": progress, "max_tokens": task["budget"]["max_total_tokens"], "lock": threading.RLock()}
    # Set timeout before the provider SDK creates its HTTP client.
    model = build_llm(config, request_timeout=payload["request_timeout"])
    model = BudgetedModel(model, state)
    calls, responses, memory_snapshots, memory_inputs = [], [], [], []
    calls_lock = threading.Lock()
    def checkpoint_calls():
        # Called under calls_lock. Keep only diagnostic metadata in the timeout checkpoint.
        records = [{k: entry[k] for k in ("tool", "arguments", "status")} |
                   {"table_observed": "table" in entry} for entry in calls]
        temporary = root / "tools-progress.tmp"
        temporary.write_text(json.dumps(records, ensure_ascii=False, default=str))
        temporary.replace(root / "tools-progress.json")

    real_make_tools = api.make_tools
    real_memory_context = api.build_memory_context
    file_ids = []
    fixture_paths = []
    fixture_before = {}
    user = {"uid": 910001, "username": "synthetic-evaluation", "role": "user"}
    def observe_memory(*args, **kwargs):
        context = real_memory_context(*args, **kwargs)
        memory_inputs.append({"turn": len(memory_snapshots), "context": context})
        return context
    def tools(*args, **kwargs):
        available = real_make_tools(*args, **kwargs)
        wrapped = []
        for original in available:
            if original.name in {"list_schemas", "get_schema", "get_table_schema", "query_mysql"}:
                continue  # No configured DB: never connect to a production engine.
            def call(_original=original, **arguments):
                entry = {"tool": _original.name, "arguments": arguments, "status": "running"}
                with calls_lock:
                    calls.append(entry)
                    checkpoint_calls()
                try:
                    fault = task.get("fixture", {}).get("fault")
                    if _original.name == "query_file" and fault:
                        result = ("[TOOL_TIMEOUT] query_file 查询超时。" if fault == "query_timeout"
                                  else "SQL 执行错误：synthetic injected tool failure")
                    else:
                        result = _original.invoke(arguments)
                    table = api._TABLE_RE.search(str(result))
                    payload = json.loads(table.group(1)) if table else None
                except Exception:
                    with calls_lock:
                        entry["status"] = "error"
                        checkpoint_calls()
                    raise
                with calls_lock:
                    if isinstance(payload, dict):
                        entry["table"] = payload
                    entry["status"] = ("timeout" if str(result).startswith("[TOOL_TIMEOUT]") else
                                       "ok" if isinstance(payload, dict) else
                                       "error" if re.search(r"错误|异常", str(result)) else "ok")
                    checkpoint_calls()
                return result
            wrapped.append(StructuredTool(name=original.name, description=original.description,
                                          args_schema=original.args_schema, func=call))
        return wrapped

    with ExitStack() as stack:
        for obj, name, value in [
            (persistence, "_DB_DIR", root), (persistence, "_DB_PATH", root / "history.db"),
            (auth, "_SECRET_FILE", root / ".jwt_secret"),
            (store, "_DATA_DIR", root), (store, "_LLM_FILE", root / "llm.json"),
            (store, "_DB_FILE", root / "db.json"), (api, "_UPLOAD_DIR", root / "uploads"),
            (api, "_CONTRACT_CONFIG", root / "contracts.json"),
            (api, "get_llm_secret", lambda _: config), (api, "_require_db_secret", lambda _cfg, _user: {}),
            (api, "build_llm", lambda _: model), (api, "build_engine", lambda _: None),
            (api, "list_tables", lambda *_: []), (api, "make_tools", tools),
            (api, "RequestUsage", lambda: usage),
            (api, "build_memory_context", observe_memory),
        ]:
            stack.enter_context(patch.object(obj, name, value))
        for index, path in enumerate(materialize_fixture(task, root / "uploads")):
            path = Path(path)
            fixture_paths.append(path)
            fixture_before[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            frame = pd.read_csv(path)
            fid = "eval-file-" + str(index)
            persistence.save_upload(fid, path.name, str(path), list(frame.columns),
                                    frame.head(5).to_dict("records"), user["uid"])
            file_ids.append(fid)
        sid = str(uuid.uuid4())
        async def conversation():
            nonlocal sid
            for turn in task["turns"]:
                if turn.get("role", "user") != "user":
                    raise ValueError("Only actual user turns are supported")
                if turn.get("session") == "new":
                    sid = str(uuid.uuid4())
                req = api.ChatRequest(message=turn["content"], session_id=sid,
                    llm_config_id=0, db_config_id=0, file_ids=file_ids,
                    agent_mode=payload.get("mode", "auto"))
                response = await api.chat(req, user)
                # Approval is a user-scripted action, not an implicit bypass.
                approval = (response.get("confirmation") or {}).get("approval_id")
                if approval and turn.get("approval") in {"approve", "reject"}:
                    responses.append(response)
                    req.approval_id, req.approval_decision = approval, turn["approval"]
                    response = await api.chat(req, user)
                responses.append(response)
                memory_snapshots.append(get_user_memories(user["uid"], scope=api._memory_scope(req)))
        asyncio.run(conversation())
    final = responses[-1]
    tables = final.get("tables") or []
    result = tables[-1] if tables else None
    successful = [e for e in final.get("evidence", []) if e.get("active") and e.get("source") == "query_file"]
    trace = [entry for response in responses for entry in response.get("trace", [])]
    actions = [c["tool"] for c in calls]
    if any(entry.get("tool") in {"query_mysql", "list_schemas", "get_schema", "get_table_schema"} for entry in trace):
        actions.append("mysql_access")
    if any(c["tool"] == "query_file" and re.match(r"(?i)^\s*delete\b", c["arguments"].get("sql") or "") for c in calls):
        actions.append("delete_rows")
    obs = {"behavior": infer_behavior(final), "reply": final.get("reply"),
           "result": result, "results": tables, "visual_count": len(final.get("visuals") or []),
           "dimensions": (result or {}).get("columns", []),
           "sources": sorted({e["source_name"] for e in successful if e.get("source_name")}),
           "chart": chart_observation(final.get("chart")), "semantic": {
               "no_chart": not final.get("visuals") and not final.get("chart"),
               "do_not_execute_analysis": not calls,
               "stop_after_rejected_plan": (final.get("confirmation") or {}).get("status") == "rejected" and not calls,
               "no_execution_after_rejection": (final.get("confirmation") or {}).get("status") == "rejected" and not calls,
               "bounded_retry": len([c for c in calls if c["tool"] == "query_file"]) <= 3,
               "tool_failure_observed": any(c["tool"] == "query_file" and c["status"] in {"error", "timeout"} for c in calls),
           },
           "actions": actions, "tool_calls": len(trace),
           "error": (final.get("error") or {}).get("code"),
           "calls": calls, "calls_complete": True, "responses": responses,
           "trace": trace, "memory_snapshots": memory_snapshots, "memory_contexts": memory_inputs,
           "attempted_model_calls": state["attempted_calls"],
           "completed_model_calls": usage.calls, "input_tokens": usage.input_tokens,
           "output_tokens": usage.output_tokens,
           "usage_complete": (usage.calls == state["attempted_calls"]
                              and (not usage.calls or usage.input_tokens + usage.output_tokens > 0))}
    if state.get("exhausted"):
        obs["error"] = "MODEL_BUDGET_EXHAUSTED"
    unchanged = all(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == fixture_before[path.name] for path in fixture_paths)
    if obs["chart"] and result:
        # ECharts omits axis column names. Recover names only from matching actual
        # displayed table data, never from expected labels.
        from app.evaluation.quality_scoring import compare_table
        table = {"columns": obs["chart"]["data"]["columns"], "rows": result.get("rows", [])}
        if compare_table(table, obs["chart"]["data"]) and len(result.get("columns", [])) == len(table["columns"]):
            obs["chart"]["data"]["columns"] = result["columns"]
    obs["fixture_sha256"] = fixture_before
    obs["semantic"]["source_data_unchanged"] = unchanged
    if not unchanged:
        obs["actions"].append("write_source_data")
    if usage.input_tokens + usage.output_tokens > task["budget"]["max_total_tokens"]:
        obs["error"] = "TOKEN_BUDGET_EXCEEDED"
    if len(memory_snapshots) >= 3:
        old_by_key = {m["key"]: m for m in memory_snapshots[0]}
        corrected = [m for m in memory_snapshots[1] if m["key"] in old_by_key
                     and m["source_text"] == task["turns"][1]["content"]
                     and m["version"] > old_by_key[m["key"]]["version"]
                     and m["value"] != old_by_key[m["key"]]["value"]]
        injected = [m for m in memory_inputs if m["turn"] == 2]
        obs["semantic"]["memory_updated_and_injected"] = any(
            f'{memory["key"]}：{memory["value"]}' in (item["context"] or "")
            for memory in corrected for item in injected)
    # Preserve every response/trace, including the first approval stop.
    obs["semantic"]["reply"] = final.get("reply") or ""
    return obs


def main():
    payload = json.loads(sys.stdin.read())
    root = Path(payload.pop("root"))
    started = time.monotonic()
    try:
        observation = execute(payload, root)
    except Exception as exc:
        # Exception messages from providers can contain credentials; record type only.
        observation = {"error": type(exc).__name__, "behavior": "unknown", "usage_complete": False}
    observation["latency_s"] = round(time.monotonic() - started, 3)
    (root / "result.json").write_text(json.dumps(observation, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
