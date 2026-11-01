"""Request-scoped usage includes planner, nested workers and memory maintenance."""
from threading import Lock
from langchain_core.callbacks import BaseCallbackHandler


class RequestUsage(BaseCallbackHandler):
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._seen = set()
        self._lock = Lock()
        self._sink = None
        self._emitted_input = 0
        self._emitted_output = 0

    def on_llm_end(self, response, *, run_id=None, **kwargs):
        with self._lock:
            if run_id is not None and run_id in self._seen:
                return
            self._seen.add(run_id)
            self.calls += 1
            for group in getattr(response, "generations", []) or []:
                for generation in group:
                    message = getattr(generation, "message", None)
                    usage = getattr(message, "usage_metadata", None) or {}
                    self.input_tokens += int(usage.get("input_tokens") or 0)
                    self.output_tokens += int(usage.get("output_tokens") or 0)
        self._flush()

    def finalize(self, sink):
        """Flush now and persist late worker completions as incremental usage rows."""
        with self._lock:
            self._sink = sink
        self._flush()

    def _flush(self):
        with self._lock:
            if self._sink is None:
                return
            input_delta = self.input_tokens - self._emitted_input
            output_delta = self.output_tokens - self._emitted_output
            if not (input_delta or output_delta):
                return
            sink = self._sink
            # Reserve deltas before releasing the lock; late completions cannot double bill.
            self._emitted_input = self.input_tokens
            self._emitted_output = self.output_tokens
        sink(input_delta, output_delta)


def with_request_usage(llm, usage):
    # Cached provider objects must never be mutated across concurrent requests.
    if hasattr(llm, "model_copy"):
        return llm.model_copy(update={"callbacks": [usage]})
    return llm
