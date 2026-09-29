"""The single worker thread that owns Cura's scene. Pure Python.

Cura has one scene, so everything that materialises a job in it (placement validation, slicing)
runs here, one task at a time. Tasks run OFF the main thread: they call MainThreadRunner for each
step that touches Cura and wait (e.g. for a file to load) without blocking Qt's event loop.

Two priorities: short interactive tasks (placement, auto-orientation) go before slices that have
not started yet. A task that is running is never interrupted.
"""

import concurrent.futures
import itertools
import queue
import threading
import traceback
from typing import Any, Callable, Optional

from .errors import ApiError

LogFunction = Callable[[str, str], None]

PRIORITY_INTERACTIVE = 0
PRIORITY_SLICE = 10
_PRIORITY_STOP = -1


class SceneWorker:
    def __init__(self, log: LogFunction) -> None:
        self._log = log
        self._queue = queue.PriorityQueue()  # type: queue.PriorityQueue
        self._sequence = itertools.count()  # FIFO order within one priority.
        self._thread = None  # type: Optional[threading.Thread]
        self._busy = threading.Event()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target = self._loop, name = "RemoteWebControlScene", daemon = True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            self._queue.put((_PRIORITY_STOP, next(self._sequence), None))
            thread.join(timeout = timeout)

    def submit(self, func: Callable[..., Any], *args: Any, priority: int = PRIORITY_INTERACTIVE,
               **kwargs: Any) -> concurrent.futures.Future:
        future = concurrent.futures.Future()  # type: concurrent.futures.Future
        self._queue.put((priority, next(self._sequence), (future, func, args, kwargs)))
        return future

    def call(self, func: Callable[..., Any], *args: Any, timeout: float = 600.0, **kwargs: Any) -> Any:
        """Runs an interactive task and waits for its result."""
        future = self.submit(func, *args, **kwargs)
        try:
            return future.result(timeout = timeout)
        except concurrent.futures.TimeoutError:
            if future.cancel():
                raise ApiError(503, "scene_busy", "Cura's scene is busy with other jobs; try again later.")
            raise ApiError(504, "scene_timeout", "The operation in Cura is taking too long; it keeps running in the background.")

    def _loop(self) -> None:
        while True:
            _, _, item = self._queue.get()
            if item is None:
                return
            future, func, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            self._busy.set()
            try:
                future.set_result(func(*args, **kwargs))
            except BaseException as e:
                if not isinstance(e, ApiError):
                    self._log("e", "[RemoteWebControl] Scene task failed:\n" + traceback.format_exc())
                future.set_exception(e)
            finally:
                self._busy.clear()
