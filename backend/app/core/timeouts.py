"""运行时超时预算：把 Worker 的软超时下沉到真实 I/O 边界。"""

# Worker 总预算仍由 agent.multi_agent 负责；以下值必须小于它，
# 这样底层请求会先自行返回，外层线程超时只作为最终保险。
LLM_REQUEST_TIMEOUT_SECONDS = 20.0
LLM_MAX_RETRIES = 0

DB_CONNECT_TIMEOUT_SECONDS = 5
DB_READ_TIMEOUT_SECONDS = 20
DB_WRITE_TIMEOUT_SECONDS = 5
DB_POOL_TIMEOUT_SECONDS = 5

# MySQL 仅对只读 SELECT 生效。query_mysql 已经在工具边界禁止写操作。
MYSQL_MAX_EXECUTION_TIME_MS = DB_READ_TIMEOUT_SECONDS * 1000

# Matplotlib、DuckDB、CSV/Excel 解析等本地任务在独立进程中执行。
LOCAL_TOOL_TIMEOUT_SECONDS = 15.0
LOCAL_TOOL_TERMINATE_GRACE_SECONDS = 1.0

# 单个应用进程内允许同时运行的角色子图数；满载时快速返回而非无限排队。
MAX_CONCURRENT_WORKERS = 4
