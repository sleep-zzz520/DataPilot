from sqlalchemy import text

SYS = "('information_schema','mysql','performance_schema','sys')"

def _q(n: str) -> str:
    return f"`{n.replace('`', '``')}`"   # 专治 share-order 这类横杠名

def list_business_schemas(engine, prefix: str = "share-") -> list:
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
            f"WHERE SCHEMA_NAME NOT IN {SYS} AND SCHEMA_NAME LIKE :p"
        ), {"p": f"{prefix}%"}).fetchall()
    return [r[0] for r in rows]

def list_tables(engine, schema=None, prefix: str = "share-") -> list:
    targets = [schema] if schema else list_business_schemas(engine, prefix)
    out = []
    with engine.connect() as c:
        for s in targets:
            rows = c.execute(text(
                "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = :s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
            ), {"s": s}).fetchall()
            for tname, tcom in rows:
                out.append({"name": f"{s}.{tname}", "comment": tcom or ""})
    return out

def get_table_schema_text(engine, schema: str, tables: list[str]) -> str:
    """只加载指定表的字段，供分析计划按需补充 Schema。"""
    if not schema or not tables:
        return "(未指定表)"
    wanted = list(dict.fromkeys(tables))
    placeholders = ", ".join(f":t{i}" for i in range(len(wanted)))
    params = {"s": schema, **{f"t{i}": table for i, table in enumerate(wanted)}}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME IN ({placeholders}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        ), params).fetchall()
        table_comments = c.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA=:s AND TABLE_TYPE='BASE TABLE' AND TABLE_NAME IN ({placeholders})"
        ), params).fetchall()
    comments = {name: comment or "" for name, comment in table_comments}
    lines = []
    for table in wanted:
        if table not in comments and not any(row[0] == table for row in rows):
            continue
        lines.append(f"- 表 {_q(schema)}.{_q(table)}" + (f"  -- {comments.get(table)}" if comments.get(table) else ""))
        for tname, cname, ctype, ccom, ckey in rows:
            if tname != table:
                continue
            tag = " [PK]" if ckey == "PRI" else (" [IDX]" if ckey else "")
            lines.append(f"    * {cname} {ctype}{tag}" + (f"  -- {ccom}" if ccom else ""))
    return "\n".join(lines) or "(未找到指定表)"

def get_tables_meta(engine, schema: str, tables: list[str]) -> dict:
    """Structured metadata for selected tables; live database remains the authority."""
    if not schema or not tables:
        return {}
    wanted = [str(table) for table in dict.fromkeys(tables) if table]
    if not wanted:
        return {}
    placeholders = ", ".join(f":t{i}" for i in range(len(wanted)))
    params = {"s": schema, **{f"t{i}": table for i, table in enumerate(wanted)}}
    with engine.connect() as c:
        cols = c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME IN ({placeholders}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        ), params).fetchall()
        table_comments = c.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA=:s AND TABLE_TYPE='BASE TABLE' AND TABLE_NAME IN ({placeholders})"
        ), params).fetchall()
        keys = c.execute(text(
            "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.STATISTICS "
            f"WHERE TABLE_SCHEMA=:s AND TABLE_NAME IN ({placeholders}) AND NON_UNIQUE=0 "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
        ), params).fetchall()
    comments = {name: comment or "" for name, comment in table_comments}
    columns: dict = {}
    for table, column, column_type, column_comment, column_key in cols:
        columns.setdefault(table, []).append({
            "name": column,
            "type": column_type,
            "comment": column_comment or "",
            "key": column_key or "",
        })
    unique_keys: dict = {}
    for table, index_name, column in keys:
        unique_keys.setdefault(table, {}).setdefault(index_name, []).append(column)
    result = {}
    for table in wanted:
        if table not in comments and table not in columns:
            continue
        result[table] = {
            "name": f"{schema}.{table}",
            "schema": schema,
            "table": table,
            "comment": comments.get(table, ""),
            "columns": columns.get(table, []),
            "unique_keys": list(unique_keys.get(table, {}).values()),
        }
    return result


def get_schema_text(engine, schema=None, prefix: str = "share-") -> str:
    targets = [schema] if schema else list_business_schemas(engine, prefix)
    return "\n\n".join(_render_one(engine, s) for s in targets) or "(无业务库)"

def _render_one(engine, schema: str) -> str:
    with engine.connect() as c:
        tables = c.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA=:s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
        ), {"s": schema}).fetchall()
        cols = c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        ), {"s": schema}).fetchall()
    col_map = {}
    for tname, cname, ctype, ccom, ckey in cols:
        col_map.setdefault(tname, []).append((cname, ctype, ccom, ckey))
    lines = [f"### 库 {_q(schema)}"]
    for tname, tcom in tables:
        lines.append(f"- 表 {_q(schema)}.{_q(tname)}" + (f"  -- {tcom}" if tcom else ""))
        for cname, ctype, ccom, ckey in col_map.get(tname, []):
            tag = " [PK]" if ckey == "PRI" else (" [IDX]" if ckey else "")
            lines.append(f"    * {cname} {ctype}{tag}" + (f"  -- {ccom}" if ccom else ""))
    return "\n".join(lines)
