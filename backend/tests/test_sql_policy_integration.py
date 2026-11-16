"""SQL policy integration through agent tools."""
import pandas as pd

from app.tools.agent_tools import make_tools
from app.tools.file_tool import make_file_tools


def _mysql_tool(**kwargs):
    return {tool.name: tool for tool in make_tools(None, **kwargs)}["query_mysql"]


def test_query_mysql_blocks_dangerous_functions_and_locking():
    tool = _mysql_tool()
    assert "禁止使用函数 SLEEP" in tool.invoke({"sql": "SELECT SLEEP(10)"})
    assert "禁止使用函数 LOAD_FILE" in tool.invoke({"sql": "SELECT LOAD_FILE('/etc/passwd')"})
    assert "禁止 SELECT FOR UPDATE" in tool.invoke({"sql": "SELECT * FROM t FOR UPDATE"})
    assert "SQL 解析失败" in tool.invoke({"sql": "SELECT * FROM t INTO OUTFILE '/tmp/x'"})


def test_query_mysql_enforces_semantic_schema_and_table_scope():
    tool = _mysql_tool(allowed_schemas={"share-order"})
    out = tool.invoke({"sql": "SELECT * FROM `share-report`.`orders`"})
    assert "不在本次授权范围内" in out

    tool = _mysql_tool(allowed_tables={"share-order.orders"})
    out = tool.invoke({"sql": "SELECT * FROM `share-order`.`customers`"})
    assert "不在本次授权范围内" in out


def test_query_file_only_allows_df_and_blocks_dangerous_functions():
    frame = pd.DataFrame({"a": [1, 2]})
    tools = {tool.name: tool for tool in make_file_tools({"sales.csv": frame})}
    out = tools["query_file"].invoke({"file": "sales.csv", "sql": "SELECT * FROM other"})
    assert "不在本次授权范围内" in out
    out = tools["query_file"].invoke({"file": "sales.csv", "sql": "SELECT LOAD_FILE('/etc/passwd') FROM df"})
    assert "禁止使用函数 LOAD_FILE" in out
