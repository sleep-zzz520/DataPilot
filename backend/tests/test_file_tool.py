"""上传文件分析工具（tools/file_tool.py）单测：duckdb 内存查询，无外部 IO。"""
import json

import pandas as pd
import pytest

from app.core.process_executor import LocalToolTimeoutError
from app.tools import file_tool
from app.tools.file_tool import make_file_tools, _to_md


def _sample_df():
    return pd.DataFrame({"科目": ["语文", "数学", "英语"], "分数": [88, 92, 78]})


def _tools(files=None):
    return make_file_tools(files or {"成绩.csv": _sample_df()})


def test_no_files_registers_nothing():
    assert make_file_tools({}) == []


def test_tools_registered():
    names = [t.name for t in _tools()]
    assert names == ["list_files", "query_file", "file_stats"]


def test_list_files():
    out = _tools()[0].invoke({})
    assert "成绩.csv" in out and "3 行" in out and "科目" in out


def test_query_file_basic():
    out = _tools()[1].invoke({"file": "成绩.csv", "sql": "SELECT 科目, 分数 FROM df"})
    assert "语文" in out and "| 88 |" in out
    assert "<!--TABLE:" in out
    machine = json.loads(out.split("<!--TABLE:")[1].rstrip("-->"))
    assert machine["columns"] == ["科目", "分数"]
    assert len(machine["rows"]) == 3


def test_query_file_unknown_file():
    out = _tools()[1].invoke({"file": "不存在.csv", "sql": "SELECT 1"})
    assert "找不到文件" in out


def test_query_file_rejects_non_select():
    out = _tools()[1].invoke({"file": "成绩.csv", "sql": "DROP TABLE df"})
    assert "只支持 SELECT" in out


def test_query_file_rejects_multiple_statements():
    out = _tools()[1].invoke({"file": "成绩.csv", "sql": "SELECT 1; SELECT 2"})
    assert "分号" in out


def test_query_file_adds_limit(monkeypatch):
    # 600 行数据不带 LIMIT → 自动追加 LIMIT 500（结果 500 行），
    # 且 markdown 输出被压缩（展示 50 行、省略 450 行）
    df = pd.DataFrame({"idx": range(600)})
    tools = make_file_tools({"big.csv": df})
    out = tools[1].invoke({"file": "big.csv", "sql": "SELECT idx FROM df"})
    assert "已省略 450 行" in out  # LIMIT 500 生效 + 长输出压缩
    machine = json.loads(out.split("<!--TABLE:")[1].rstrip("-->"))
    assert len(machine["rows"]) == 200  # 前端表格数据完整


def test_query_file_sql_error_returns_friendly():
    out = _tools()[1].invoke({"file": "成绩.csv", "sql": "SELECT 不存在的列 FROM df"})
    assert "SQL 执行错误" in out


def test_file_stats():
    out = _tools()[2].invoke({"file": "成绩.csv"})
    assert "3 行" in out and "均值" in out and "科目" in out


def test_to_md_empty():
    md = _to_md(pd.DataFrame(columns=["a", "b"]))
    assert "| (空结果) |" in md


def test_to_md_escapes_pipe():
    df = pd.DataFrame({"col": ["a|b"]})
    assert "a\\|b" in _to_md(df)


def test_to_md_handles_nan():
    df = pd.DataFrame({"col": [1, None]})
    md = _to_md(df)
    assert "nan" not in md.lower()


def test_to_md_max_rows_omits_and_notes():
    df = pd.DataFrame({"idx": range(100)})
    md = _to_md(df, max_rows=50)
    assert "已省略 50 行" in md and "仅展示前 50 行" in md
    assert "| 49 |" in md and "| 50 |" not in md


def test_query_file_compresses_long_output():
    df = pd.DataFrame({"idx": range(600)})
    tools = {t.name: t for t in make_file_tools({"big.csv": df})}
    out = tools["query_file"].invoke({"file": "big.csv", "sql": "SELECT idx FROM df"})
    assert "已省略" in out
    machine = json.loads(out.split("<!--TABLE:")[1].rstrip("-->"))
    assert len(machine["rows"]) == 200  # 前端表格仍完整


def test_query_file_uses_isolated_process_for_persisted_upload(tmp_path):
    path = tmp_path / "scores.csv"
    _sample_df().to_csv(path, index=False)
    tools = {item.name: item for item in make_file_tools(
        {"成绩.csv": _sample_df()},
        file_paths={"成绩.csv": str(path)},
    )}

    out = tools["query_file"].invoke({"file": "成绩.csv", "sql": "SELECT 科目, 分数 FROM df"})

    assert "语文" in out and "<!--TABLE:" in out


def test_query_file_process_timeout_is_machine_readable(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise LocalToolTimeoutError("too slow")

    monkeypatch.setattr(file_tool, "run_in_process", timeout)
    path = tmp_path / "scores.csv"
    _sample_df().to_csv(path, index=False)
    tools = {item.name: item for item in make_file_tools(
        {"成绩.csv": _sample_df()},
        file_paths={"成绩.csv": str(path)},
    )}

    out = tools["query_file"].invoke({"file": "成绩.csv", "sql": "SELECT * FROM df"})
    assert out.startswith("[TOOL_TIMEOUT] query_file")
