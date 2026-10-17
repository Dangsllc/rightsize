"""Systems that fail in specific ways, for exercising how runs end and how the Workbench shows it."""

import asyncio
import hashlib

from rightsize_core.schema import Outcome, OutcomeError, SystemInfo


class Scripted:
    def __init__(self, error_share: float = 0.0, delay: float = 0.0, message: str = "") -> None:
        self.error_share, self.delay, self.message = error_share, delay, message

    def info(self):
        return SystemInfo(name="scripted", task_types=["retrieval"], supports_model_param=True)

    async def run(self, task_type, inputs, model, params):
        await asyncio.sleep(self.delay)
        roll = int(hashlib.sha256(inputs["query"].encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        if roll < self.error_share:
            return Outcome(error=OutcomeError(kind="error", message=self.message))
        return Outcome(prediction={"hits": [{"citation": "164.308(a)(1)(ii)(A)", "score": 1.0}]})


UNAUTHORIZED = ("HTTPStatusError(\"Client error '401 Unauthorized' for url "
                "'http://127.0.0.1:1234/v1/chat/completions'\\nFor more information check: ...\")")


def failing():
    """Every call is rejected, like a server whose key is wrong."""
    return Scripted(error_share=1.0, message=UNAUTHORIZED)


def flaky():
    """About one call in ten times out."""
    return Scripted(error_share=0.1, message="ReadTimeout('timed out')")


def slow():
    return Scripted(delay=0.2)
