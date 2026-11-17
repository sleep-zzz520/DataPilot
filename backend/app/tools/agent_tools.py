import json, re, time
import pandas as pd
import sqlglot
from langchain_core.tools import tool
from sqlglot import exp
from app.core.timeouts import MYSQL_MAX_EXECUTION_TIME_MS
from app.db.query_cache import cacheable_sql, get_query_cache
from app.db.query_runtime import record_query_runtime
from app.db.schema import _q, list_business_schemas
from app.db.schema_cache import get_schema_cache
from app.db.sql_policy import SqlPolicyError, policy_message, validate_readonly_sql
from app.security.data_policy import load_data_policy
from app.tools.chart_tool import get_chart_tools
from app.tools.file_tool import make_file_tools
from app.tools.statistical_tool import make_statistical_tools

_BARE_DASH = re.compile(r"(?<!`)(share-[A-Za-z0-9_]+)(?!`)")

# 长工具输出压缩：查询结果 markdown 最多展示的行数（省 token，前端表格仍取 machine rows）
_TOOL_MAX_ROWS = 50


def _is_timeout_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "timeout", "timed out", "max_execution_time",
        "maximum statement execution time", "query execution was interrupted",
    ))


def _uses_unqualified_table(sql: str) -> bool:
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return False
    cte_names = {cte.alias for cte in statement.find_all(exp.CTE) if cte.alias}
    return any(not table.db and table.name not in cte_names
               for table in statement.find_all(exp.Table))


def _read_mysql_with_timeout(sql: str, engine, default_schema: str = None):
    """在同一连接上选择必要默认库并设置 MySQL 的 SELECT 时限后再读取数据。"""
    # engine=None 是安全校验/单测路径；生产请求一定提供 SQLAlchemy Engine。
    if engine is None:
        return pd.read_sql(sql, engine)
    with engine.connect() as conn:
        try:
            if default_schema and _uses_unqualified_table(sql):
                conn.exec_driver_sql(f"USE {_q(default_schema)}")
            conn.exec_driver_sql(
                f"SET SESSION max_execution_time = {MYSQL_MAX_EXECUTION_TIME_MS}"
            )
            # Generated SQL has no bound parameters. Omit the empty DBAPI parameter
            # mapping so PyMySQL preserves DATE_FORMAT/LIKE percent signs verbatim.
            # Keep the driver SQL path: text(sql) would reinterpret colon literals.
            return pd.read_sql(sql, conn.execution_options(no_parameters=True))
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

def make_tools(engine, default_schema=None, files=None, audit_ctx=None, file_paths=None,
               allowed_schemas=None, allowed_tables=None, cache_scope=None,
               data_policy=None, sensitive_approved=False):
    """构造工具集。files: {文件名: DataFrame}，有上传文件时注册文件分析工具。

    audit_ctx: 可选 {"user_id","username","session_id"}，传入后 SQL 执行会写审计日志。
    allowed_schemas/allowed_tables: 本轮检索授权范围；为空时保持旧的兼容行为。
    cache_scope: 结果缓存命名空间（应包含用户与数据源），为空时禁用缓存。
    data_policy/sensitive_approved: 敏感表门禁与结果脱敏；审批通过后才允许访问敏感表。
    """
    from app.audit import record, A_DATA_MASK, A_SENSITIVE_ACCESS, A_SQL_QUERY
    ctx = audit_ctx or {}
    schema_cache = get_schema_cache()
    data_policy = data_policy or load_data_policy()

    def _audit_sql(sql: str, rows: int, source: str, duration_ms: float,
                   masked_columns=None):
        record(A_SQL_QUERY, ctx.get("user_id"), ctx.get("username"), {
            "sql": sql, "rows": rows, "source": source, "session_id": ctx.get("session_id"),
            "duration_ms": round(max(0.0, float(duration_ms)), 2),
            "masked_columns": list(masked_columns or []),
        })

    allowed_schemas = list(allowed_schemas) if allowed_schemas is not None else None
    result_cache = get_query_cache() if cache_scope else None
    mysql_cache_scope = f"{cache_scope}|{getattr(engine, 'url', '')}" if cache_scope else None

    def _schema_allowed(name: str) -> bool:
        return allowed_schemas is None or name in allowed_schemas

    @tool
    def list_schemas() -> str:
        """列出所有业务库名。写 SQL 前必须先调用，判断问题涉及哪些库。"""
        if allowed_schemas is not None:
            return ", ".join(allowed_schemas) or "(无授权库)"
        names = list_business_schemas(engine)
        return ", ".join(names) or "(无业务库)"

    @tool
    def get_schema(schema: str) -> str:
        """查看指定库的表结构。schema 必填，如 share-order。先 list_schemas 再针对相关库调用，禁止一次看全部。"""
        if not schema:
            return "错误：请指定库名 schema（如 share-order）。先用 list_schemas 看库列表。"
        if not _schema_allowed(schema):
            return "错误：schema 不在本次授权范围内。"
        return schema_cache.get_schema_text(engine, schema)

    @tool
    def get_table_schema(schema: str, tables: list[str]) -> str:
        """只查看指定表的字段结构；已有分析计划时优先使用，避免加载整个库。"""
        if not schema or not tables:
            return "错误：schema 和 tables 都不能为空。"
        if not _schema_allowed(schema):
            return "错误：schema 不在本次授权范围内。"
        return schema_cache.get_table_schema_text(engine, schema, tables)

    @tool
    def query_mysql(sql: str) -> str:
        """执行只读 SELECT。横杠库名必须反引号全限定，如 `share-order`.`表名`。报错时据错误信息修正后重试。"""
        sql_text = (sql or "").strip()
        bare = _BARE_DASH.findall(sql_text)
        if bare and sql_text.lower().startswith("select"):
            return f"错误：{bare} 含横杠但未加反引号，请用反引号全限定，如 `share-order`.`表名`。"
        try:
            safe_sql = validate_readonly_sql(
                sql_text, dialect="mysql", default_limit=500,
                allowed_schemas=allowed_schemas, allowed_tables=allowed_tables,
                default_schema=default_schema,
            )
        except SqlPolicyError as exc:
            return policy_message(exc, tool="mysql")
        sensitive_hits = data_policy.sensitive_tables_in_sql(
            safe_sql, dialect="mysql", default_schema=default_schema,
        )
        if sensitive_hits and not sensitive_approved:
            if ctx:
                record(A_SENSITIVE_ACCESS, ctx.get("user_id"), ctx.get("username"), {
                    "action": "denied", "tables": sorted(sensitive_hits),
                    "session_id": ctx.get("session_id"),
                })
            return (
                "错误：本次查询涉及敏感表（" + "、".join(sorted(sensitive_hits))
                + "），需要用户确认后才能访问。请先向用户说明数据范围并请求确认。"
            )
        if sensitive_hits and ctx:
            record(A_SENSITIVE_ACCESS, ctx.get("user_id"), ctx.get("username"), {
                "action": "allowed", "tables": sorted(sensitive_hits),
                "session_id": ctx.get("session_id"),
            })
        cache_key = None
        if result_cache is not None and cacheable_sql(safe_sql):
            cache_key = result_cache.make_key(mysql_cache_scope, safe_sql, "mysql")
            cached = result_cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            started = time.perf_counter()
            df = _read_mysql_with_timeout(safe_sql, engine, default_schema)
            duration_ms = (time.perf_counter() - started) * 1000
            extras = data_policy.sensitive_columns_for_sql(safe_sql, dialect="mysql")
            df, masked_columns = data_policy.mask_dataframe(df, extras)
            if masked_columns and ctx:
                record(A_DATA_MASK, ctx.get("user_id"), ctx.get("username"), {
                    "source": "mysql", "columns": masked_columns,
                    "session_id": ctx.get("session_id"),
                })
            if ctx:
                _audit_sql(safe_sql, len(df), "mysql", duration_ms, masked_columns)
            record_query_runtime("mysql", duration_ms, rows=len(df),
                                 audit_ctx=ctx, sql=safe_sql)
            d2 = df.head(200)
            machine = {"columns": list(df.columns),
                       "rows": d2.where(d2.notna(), None).values.tolist(),
                       "executed_sql": safe_sql, "source": "mysql",
                       "row_count_total": len(df), "truncated": len(df) > 200}
            output = _to_md(df, max_rows=_TOOL_MAX_ROWS) + f"\n<!--TABLE:{json.dumps(machine, ensure_ascii=False, default=str)}-->"
            if cache_key is not None:
                result_cache.set(cache_key, output)
            return output
        except Exception as e:
            duration_ms = (time.perf_counter() - started) * 1000
            record_query_runtime("mysql", duration_ms, ok=False,
                                 audit_ctx=ctx, sql=safe_sql)
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
    file_tools = make_file_tools(files or {}, audit_ctx=ctx, file_paths=file_paths,
                                 cache_scope=cache_scope, data_policy=data_policy)
    # 统计工具只由 statistical_validator 子图使用；LLM 选择方法，Python/numpy 计算数值。
    statistical_tools = make_statistical_tools()

    return [list_schemas, get_schema, get_table_schema, query_mysql, make_chart] + chart_tools + file_tools + statistical_tools
