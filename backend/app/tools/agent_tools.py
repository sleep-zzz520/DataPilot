import json, re
import pandas as pd
from langchain_core.tools import tool
from app.core.timeouts import MYSQL_MAX_EXECUTION_TIME_MS
from app.db.schema import list_business_schemas, get_schema_text, get_table_schema_text
from app.tools.chart_tool import get_chart_tools
from app.tools.file_tool import make_file_tools
from app.tools.statistical_tool import make_statistical_tools

_FORBIDDEN = re.compile(r"\b(drop|delete|update|insert|alter|truncate|create|grant|revoke)\b", re.I)
_BARE_DASH = re.compile(r"(?<!`)(share-[A-Za-z0-9_]+)(?!`)")

# 长工具输出压缩：查询结果 markdown 最多展示的行数（省 token，前端表格仍取 machine rows）
_TOOL_MAX_ROWS = 50


def _is_timeout_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "timeout", "timed out", "max_execution_time",
        "maximum statement execution time", "query execution was interrupted",
    ))


def _read_mysql_with_timeout(sql: str, engine):
    """在同一连接上设置 MySQL 的 SELECT 时限后再读取数据。"""
    # engine=None 是安全校验/单测路径；生产请求一定提供 SQLAlchemy Engine。
    if engine is None:
        return pd.read_sql(sql, engine)
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql(
                f"SET SESSION max_execution_time = {MYSQL_MAX_EXECUTION_TIME_MS}"
            )
            return pd.read_sql(sql, conn)
        except Exception as exc:
            # read_timeout 可能留下半断开的 socket，不能把它归还给连接池复用。
            if _is_timeout_error(exc):
                conn.invalidate(exc)
            raise

def _to_md(df, max_rows: int = None):
    """DataFrame → Markdown 表格。max_rows 限制正文行数，超出省略并提示（压缩长输出）。"""
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

def make_tools(engine, default_schema=None, files=None, audit_ctx=None, file_paths=None):
    """构造工具集。files: {文件名: DataFrame}，有上传文件时注册文件分析工具。

    audit_ctx: 可选 {"user_id","username","session_id"}，传入后 SQL 执行会写审计日志。
    """
    from app.audit import record, A_SQL_QUERY
    ctx = audit_ctx or {}

    def _audit_sql(sql: str, rows: int, source: str):
        record(A_SQL_QUERY, ctx.get("user_id"), ctx.get("username"), {
            "sql": sql, "rows": rows, "source": source, "session_id": ctx.get("session_id"),
        })

    @tool
    def list_schemas() -> str:
        """列出所有业务库名。写 SQL 前必须先调用，判断问题涉及哪些库。"""
        names = list_business_schemas(engine)
        return ", ".join(names) or "(无业务库)"

    @tool
    def get_schema(schema: str) -> str:
        """查看指定库的表结构。schema 必填，如 share-order。先 list_schemas 再针对相关库调用，禁止一次看全部。"""
        if not schema:
            return "错误：请指定库名 schema（如 share-order）。先用 list_schemas 看库列表。"
        return get_schema_text(engine, schema)

    @tool
    def get_table_schema(schema: str, tables: list[str]) -> str:
        """只查看指定表的字段结构；已有分析计划时优先使用，避免加载整个库。"""
        if not schema or not tables:
            return "错误：schema 和 tables 都不能为空。"
        return get_table_schema_text(engine, schema, tables)

    @tool
    def query_mysql(sql: str) -> str:
        """执行只读 SELECT。横杠库名必须反引号全限定，如 `share-order`.`表名`。报错时据错误信息修正后重试。"""
        if _FORBIDDEN.search(sql):
            return "错误：仅允许 SELECT，检测到写操作关键字。"
        if not sql.strip().lower().startswith("select"):
            return "错误：SQL 必须以 SELECT 开头。"
        bare = _BARE_DASH.findall(sql)
        if bare:
            return f"错误：{bare} 含横杠但未加反引号，请用反引号全限定，如 `share-order`.`表名`。"
        if "limit" not in sql.lower():
            sql = sql.rstrip(";") + " LIMIT 500"
        try:
            df = _read_mysql_with_timeout(sql, engine)
            if ctx:
                _audit_sql(sql, len(df), "mysql")
            d2 = df.head(200)
            machine = {"columns": list(df.columns),
                       "rows": d2.where(d2.notna(), None).values.tolist()}
            return _to_md(df, max_rows=_TOOL_MAX_ROWS) + f"\n<!--TABLE:{json.dumps(machine, ensure_ascii=False, default=str)}-->"
        except Exception as e:
            if _is_timeout_error(e):
                return (
                    "[TOOL_TIMEOUT] query_mysql 查询超时：数据库未在规定时间内返回。"
                    "请缩小时间范围、增加过滤条件或优化 SQL 后重试。"
                )
            return f"SQL 执行错误：{e}  请根据此错误修正 SQL 后重试（最多 3 次）。"

    # 基础图表工具（保留兼容性）
    @tool
    def make_chart(chart_type: str, title: str, x_labels: list, series: list) -> str:
        """生成 ECharts 图表。chart_type∈bar/line/pie。bar/line：series=[{name,data:[...]}]；pie：series=[{name,value}]。"""
        ct = (chart_type or "bar").lower()
        if ct == "pie":
            option = {"title": {"text": title}, "tooltip": {"trigger": "item"}, "legend": {},
                      "series": [{"type": "pie", "radius": "60%", "data": series or []}]}
        else:
            option = {"title": {"text": title}, "tooltip": {"trigger": "axis"}, "legend": {},
                      "xAxis": {"type": "category", "data": x_labels or []}, "yAxis": {"type": "value"},
                      "series": [{"type": ct, "name": s.get("name", ""), "data": s.get("data", [])} for s in (series or [])]}
        return f"<!--CHART:{json.dumps(option, ensure_ascii=False)}-->图表已生成：{title}（前端会渲染，回复里不要重复罗列数据）"

    # 高级可视化工具
    chart_tools = get_chart_tools()

    # 上传文件分析工具（有文件时才注册）
    file_tools = make_file_tools(files or {}, audit_ctx=ctx, file_paths=file_paths)
    # 统计工具只由 statistical_validator 子图使用；LLM 选择方法，Python/numpy 计算数值。
    statistical_tools = make_statistical_tools()

    return [list_schemas, get_schema, get_table_schema, query_mysql, make_chart] + chart_tools + file_tools + statistical_tools
