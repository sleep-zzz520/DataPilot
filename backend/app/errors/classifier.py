# app/errors/classifier.py —— 纯映射逻辑，不依赖任何配置，可单测
def classify_llm_error(e: Exception) -> dict:
    code, status, body = _extract_http(e)        # 从 openai/dashscope 异常里抠 status_code 和 body
    msg = str(e)

    # —— 额度 / 计费类（用户最常被坑的）——
    if status == 402 or _has(body, "Arrearage", "insufficient_quota", "balance", "欠费", "余额"):
        return _r("LLM_QUOTA", "模型调用失败：账户余额/额度不足或已欠费。",
                  "请到模型服务商控制台充值，或在配置页切换到其它可用的 API Key。", "block")

    # —— 限流 ——
    if status == 429 or _has(body, "Throttling", "rate_limit", "限流"):
        return _r("LLM_RATE_LIMIT", "请求被限流：调用太频繁或超出每分钟配额。",
                  "稍等片刻重试；或更换更高配额的 Key / 模型。", "warn")

    # —— 鉴权 / Key 无效 ——
    if status == 401 or _has(body, "InvalidApiKey", "invalid_api_key", "Unauthorized", "鉴权"):
        return _r("LLM_AUTH", "API Key 无效、过期或无权限。",
                  "请在配置页检查并重新填写 API Key。", "block")

    # —— 模型名错 / 不支持 ——
    if status == 404 or _has(body, "ModelNotFound", "model_not_found", "does not exist"):
        return _r("LLM_MODEL", f"模型不可用：'{_safe(body)}' 对应的模型名不存在或该 Key 无权访问。",
                  "请核对模型名称（如 qwen-max / qwen-plus）是否与服务商一致。", "block")

    # —— 上下文超长 ——
    if _has(body, "context_length", "maximum context", "Range of input length", "token"):
        return _r("LLM_CONTEXT", "输入超出模型上下文长度（数据/对话太长）。",
                  "减少一次性查询的数据量，或新开一轮对话。", "warn")

    # —— 网络 / 地址不通 ——
    if _is_timeout(e):
        return _r("LLM_TIMEOUT", "模型响应超时。",
                  "请缩小问题范围后重试；若持续发生，请检查模型服务延迟。", "warn")

    if _is_conn_error(e) or _has(body, "Connection", "Name or service not known"):
        return _r("LLM_NETWORK", "无法连接到模型服务地址。",
                  "请检查 Base URL 是否正确、服务器能否访问外网/该地址。", "block")

    return _r("LLM_UNKNOWN", "模型调用失败。", f"原始信息：{msg[:200]}", "warn")


def classify_db_error(e: Exception) -> dict:
    errno = _extract_mysql_errno(e)               # PyMySQL 的 errno，如 1045/2003
    msg = str(e)

    if errno == 3024 or _is_timeout(e):
        return _r("DB_TIMEOUT", "数据库查询超时。",
                  "请缩小查询范围、增加过滤条件或优化 SQL 后重试。", "warn")

    if errno == 2003 or _is_conn_error(e):
        return _r("DB_CONNECT", "连不上数据库服务器。",
                  "请检查 IP、端口是否正确，以及防火墙/白名单是否放行了本服务。", "block")
    if errno == 1045:
        return _r("DB_AUTH", "数据库账号或密码错误。", "请重新填写用户名和密码。", "block")
    if errno == 1049:
        return _r("DB_NO_DB", "指定的数据库不存在。", "请核对库名（注意大小写）。", "block")
    if errno in (1044, 1142):
        return _r("DB_PERM", "该账号权限不足，无法读取目标库/表。",
                  "请为账号授予 SELECT 权限，或更换账号。", "block")
    if errno == 1064:
        # 语法错：不甩给用户，标记让上层回喂 LLM 自修正
        return _r("DB_SQL_SYNTAX", "生成的 SQL 有语法错误。", "正在自动修正重试…", "retry")
    if errno in (1205, 1213):
        return _r("DB_LOCK", "数据库临时繁忙（锁等待/死锁）。", "稍后自动重试。", "retry")

    return _r("DB_UNKNOWN", "数据库操作失败。", f"原始信息：{msg[:200]}", "warn")


# ---------- 工具函数 ----------
def _r(code, message, suggestion, severity):
    return {"code": code, "message": message, "suggestion": suggestion, "severity": severity}

def _has(body, *keys):  return any(k.lower() in (body or "").lower() for k in keys)
def _safe(body):        return (body or "")[:60]
def _is_conn_error(e):
    return type(e).__name__ in ("ConnectionError","ConnectTimeout","ConnectionRefusedError",
                                "OperationalError") and any(k in str(e) for k in ("Can't connect","refused","timed out","getaddrinfo"))
def _is_timeout(e):
    s = (type(e).__name__ + " " + str(e)).lower()
    return any(k in s for k in ("timeout", "timed out", "read timed out",
                                "max_execution_time", "maximum statement execution time",
                                "query execution was interrupted"))
def _extract_http(e):
    status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
    body = getattr(getattr(e, "response", None), "text", None) or getattr(e, "body", None) or str(e)
    code = getattr(e, "code", None)                  # dashscope 风格
    return status or code, status, f"{body} {code}"
def _extract_mysql_errno(e):
    orig = getattr(e, "orig", None)                  # SQLAlchemy 包裹的 PyMySQL 异常
    return getattr(orig, "args", [None])[0] if orig else None

# ===== 以下为 chat 主流程兜底分发（增量追加；上方原有 classify_llm_error / classify_db_error / 1064 等一律保留）=====
def _looks_db(e):
    """凭异常类型/文本特征粗判是否数据库异常，供 classify_any 兜底分发。"""
    s = (type(e).__name__ + str(e)).lower()
    return any(k in s for k in ("pymysql", "operationalerror", "programmingerror",
                                "interfaceerror", "mysql"))

def classify_any(e):
    """chat 主流程统一入口：能识别出 mysql errno 或 db 特征 → 归 db；否则归 llm。
       注意：你原有的 errno==1064 → DB_SQL_SYNTAX(retry) 在 classify_db_error 内，会被这里正确路由。"""
    if _extract_mysql_errno(e) is not None or _looks_db(e):
        return classify_db_error(e)
    return classify_llm_error(e)
