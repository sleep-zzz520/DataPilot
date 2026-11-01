"""Request-scoped execution limits shared by supervisor and worker graphs."""
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ExecutionBudget:
    """Atomically reserve work before starting it; workers never reset limits."""

    max_model_calls: int = 20
    max_tool_calls: int = 30
    max_query_calls: int = 3
    max_steps: int = 60
    model_calls: int = 0
    tool_calls: int = 0
    query_calls: int = 0
    steps: int = 0
    exhausted_reason: str = ""
    _lock: Lock = field(default_factory=Lock, repr=False)

    def reserve(self, kind: str, *, query: bool = False) -> bool:
        with self._lock:
            if self.exhausted_reason:
                return False
            limits = [(self.steps, self.max_steps, "执行步骤")]
            if kind == "model":
                limits.append((self.model_calls, self.max_model_calls, "模型调用"))
            else:
                limits.append((self.tool_calls, self.max_tool_calls, "工具调用"))
                if query:
                    limits.append((self.query_calls, self.max_query_calls, "查询调用"))
            for used, limit, label in limits:
                if used >= limit:
                    self.exhausted_reason = f"{label}已达到本轮硬上限 {limit}"
                    return False
            self.steps += 1
            if kind == "model":
                self.model_calls += 1
            else:
                self.tool_calls += 1
                self.query_calls += int(query)
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "query_calls": self.query_calls,
                "steps": self.steps,
                "exhausted_reason": self.exhausted_reason,
            }
