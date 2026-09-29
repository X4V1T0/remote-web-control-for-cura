import queue
import threading

import pytest

from RemoteWebControl.errors import ApiError
from RemoteWebControl.main_thread import MainThreadRunner


class FakeMainLoop:
    """Stands in for Qt's event loop: a thread that executes posted callables in order."""

    def __init__(self):
        self.calls = queue.Queue()
        self.paused = threading.Event()
        self.thread = threading.Thread(target = self._loop, daemon = True)
        self.thread.start()

    def call_later(self, func):
        self.calls.put(func)

    def _loop(self):
        while True:
            func = self.calls.get()
            if func is None:
                return
            func()

    def stop(self):
        self.calls.put(None)
        self.thread.join(timeout = 5)


@pytest.fixture
def loop():
    fake = FakeMainLoop()
    yield fake
    fake.stop()


def test_runs_on_main_thread_and_returns_result(loop):
    runner = MainThreadRunner(loop.call_later, main_thread = loop.thread)
    assert runner.run(lambda a, b: (threading.current_thread() is loop.thread, a + b), 2, b = 3) == (True, 5)


def test_propagates_exceptions(loop):
    runner = MainThreadRunner(loop.call_later, main_thread = loop.thread)

    def boom():
        raise ApiError(404, "printer_not_found", "nope")

    with pytest.raises(ApiError) as info:
        runner.run(boom)
    assert info.value.code == "printer_not_found"


def test_timeout_cancels_the_call(loop):
    runner = MainThreadRunner(loop.call_later, main_thread = loop.thread)
    blocker = threading.Event()
    executed = []
    loop.call_later(blocker.wait)  # Keep the "main thread" busy.

    with pytest.raises(ApiError) as info:
        runner.run(lambda: executed.append(True), timeout = 0.1)
    assert info.value.status == 503

    blocker.set()
    runner.run(lambda: None)  # Drain the queue.
    assert executed == []  # The cancelled call never ran.


def test_direct_call_when_already_on_main_thread():
    runner = MainThreadRunner(lambda f: pytest.fail("must not post"), main_thread = threading.current_thread())
    assert runner.run(lambda: 42) == 42
