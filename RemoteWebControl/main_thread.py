"""Run callables on Cura's main (Qt) thread from other threads and wait for the result.

Pure Python: the Cura dependency is injected as `call_later` (CuraApplication.callLater,
which posts a Qt event and is safe to call from any thread).
"""

import concurrent.futures
import threading
from typing import Any, Callable, Optional

from .errors import ApiError


class MainThreadRunner:
    def __init__(self, call_later: Callable[..., None], main_thread: Optional[threading.Thread] = None,
                 default_timeout: float = 30.0) -> None:
        self._call_later = call_later
        self._main_thread = main_thread or threading.main_thread()
        self._default_timeout = default_timeout

    def run(self, func: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """Runs func(*args, **kwargs) on the main thread and returns its result.

        Exceptions raised by func are re-raised in the calling thread. If the main thread does not
        pick up the call within the timeout, the call is cancelled and a 503 ApiError is raised.
        """
        if threading.current_thread() is self._main_thread:
            return func(*args, **kwargs)

        future = concurrent.futures.Future()  # type: concurrent.futures.Future

        def invoke() -> None:
            if not future.set_running_or_notify_cancel():
                return  # The caller gave up waiting; don't touch Cura any more.
            try:
                future.set_result(func(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

        self._call_later(invoke)
        try:
            return future.result(timeout = self._default_timeout if timeout is None else timeout)
        except concurrent.futures.TimeoutError:
            if future.cancel():
                raise ApiError(503, "main_thread_timeout", "Cura did not respond in time. Is it busy or showing a dialog?")
            # It started running just now; it's too late to cancel, so wait for it.
            return future.result()
