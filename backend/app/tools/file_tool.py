"""上传文件分析工具 - 让 agent 真实读取 CSV/Excel 文件数据（pandas + duckdb）。

上传的文件在 upload 阶段被 pandas 解析为 DataFrame 并暂存；
这些工具让 LLM 用 SQL 直接查询文件数据（而非靠列名/预览猜测），
查询结果可进一步交给图表工具生成可视化。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import pandas as pd
import duckdb
from langchain_core.tools import tool

# 长工具输出压缩：查询结果 markdown 最多展示的行数（省 token）
_TOOL_MAX_ROWS = 50


def _to_md(df: pd.DataFrame, max_rows: int = None) -> str:
    """DataFrame → Markdown 表格（与 mysql_tool 的 query 输出格式一致）。
    max_rows 限制正文行数，超出省略并提示（压缩长工具输出，省 token）。"""
    cols = list(df.columns)
    esc = lambda v: "" if pd.isna(v) else str(v).replace("|", "\\|")
    head = "| " + " | ".join(esc(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    if len(df) == 0:
        return head + "\n" + sep + "\n| (空结果) |"
    rows = df.values.tolist()
    omitted = len(rows) - max_rows if max_rows and len(rows) > max_rows else 0
    if omitted > 0:
        rows = rows[:max_rows]
    body = "\n".join("| " + " | ".join(esc(v) for v in row) + " |" for row in rows)
    if omitted > 0:
        body += f"\n| …（共 {len(df)} 行，已省略 {omitted} 行，仅展示前 {max_rows} 行） |"
    return "\n".join([head, sep, body])


def make_file_tools(files: Dict[str, pd.DataFrame], audit_ctx: Optional[Dict] = None) -> List:
    """根据当前对话的上传文件构造文件分析工具。

    files: {文件名: DataFrame}
    audit_ctx: 可选 {"user_id","username","session_id"}，传入后 SQL 执行会写审计日志。
    """
    if not files:
        return []

    from app.audit import record, A_SQL_QUERY
    ctx = audit_ctx or {}

    def _audit_query(sql: str, rows: int, file: str):
        record(A_SQL_QUERY, ctx.get("user_id"), ctx.get("username"), {
            "sql": sql, "rows": rows, "source": f"file:{file}", "session_id": ctx.get("session_id"),
        })

    @tool
    def list_files() -> str:
        """列出当前对话可用的上传文件：文件名、行数、列名。分析上传文件前必须先调用。"""
        lines = [f"（{len(files)} 个文件）"]
        for name, df in files.items():
            lines.append(f"- {name}：{len(df)} 行 × {len(df.columns)} 列，列：{list(df.columns)}")
        return "\n".join(lines)

    @tool
    def query_file(file: str, sql: str) -> str:
        """对上传的 CSV/Excel 文件执行只读 SQL 查询，返回结果表格。

        参数：
        - file：文件名，必须是 list_files 返回的文件名
        - sql：SELECT 查询。表名统一用 df（忽略文件名）。
          示例：SELECT 科目, AVG(分数) AS 平均分 FROM df GROUP BY 科目 ORDER BY 平均分 DESC
        只允许单条 SELECT；结果自动限制 500 行。
        """
        df = files.get(file)
        if df is None:
            return f"错误：找不到文件「{file}」。可用文件：{list(files.keys())}"
        sql2 = (sql or "").strip().rstrip(";")
        if not sql2.lower().startswith("select"):
            return "错误：query_file 只支持 SELECT 查询。"
        if ";" in sql2:
            return "错误：只允许单条 SQL，不要使用分号。"
        if "limit" not in sql2.lower():
            sql2 += " LIMIT 500"
        try:
            con = duckdb.connect(":memory:")
            con.execute("SET enable_external_access=false")  # 禁止读取外部文件
            con.register("df", df)
            res = con.execute(sql2).fetchdf()
            con.close()
            if ctx:
                _audit_query(sql2, len(res), file)
            machine = {
                "columns": list(res.columns),
                "rows": res.head(200).where(res.notna(), None).values.tolist(),
            }
            return _to_md(res, max_rows=_TOOL_MAX_ROWS) + f"\n<!--TABLE:{json.dumps(machine, ensure_ascii=False, default=str)}-->"
        except Exception as e:
            return f"SQL 执行错误：{e}  请修正 SQL 后重试。"

    @tool
    def file_stats(file: str) -> str:
        """返回上传文件的统计摘要：数值列的均值/最大/最小/中位数/行数，分类列的取值数。"""
        df = files.get(file)
        if df is None:
            return f"错误：找不到文件「{file}」。可用文件：{list(files.keys())}"
        num = df.select_dtypes(include=["number"])
        obj = df.select_dtypes(exclude=["number"])
        lines = [f"文件「{file}」统计：{len(df)} 行 × {len(df.columns)} 列，缺失值 {int(df.isna().sum().sum())} 个"]
        if len(num.columns):
            stats = num.agg(["mean", "min", "max", "median"]).T.reset_index()
            stats.columns = ["列", "均值", "最小值", "最大值", "中位数"]
            stats = stats.round(2)
            lines.append("\n数值列：\n" + _to_md(stats))
        if len(obj.columns):
            lines.append("\n分类列取值数：")
            for c in obj.columns[:8]:
                lines.append(f"- {c}：{obj[c].nunique()} 个不同值，缺失 {int(obj[c].isna().sum())} 行")
        return "\n".join(lines)

    return [list_files, query_file, file_stats]
