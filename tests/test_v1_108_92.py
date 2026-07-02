"""v1.108.92 — progress notification flood control + response-trailing drain (#359).

A full-repo index_folder emitted one notifications/progress per file
(~2,000 in ~4s on a 2,000-file walk). Claude Code loses the tool result
under that flood, and a notification written after the response makes it
drop the whole stdio connection ("progress notification for an unknown
token"). Fixes: ProgressReporter throttles (min 1% step + min 100ms gap,
final send exempt) and the dispatcher drains in-flight sends before the
response is returned.
"""

import asyncio
import threading

from jcodemunch_mcp.progress import (
    PROGRESS_MIN_INTERVAL_S,
    PROGRESS_MIN_STEP,
    ProgressReporter,
    drain_reporter,
    make_progress_notify,
)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeFuture:
    def __init__(self, is_done: bool = True) -> None:
        self._done = is_done

    def done(self) -> bool:
        return self._done


def _collecting_notify(calls):
    def _notify(progress, total, message):
        calls.append((progress, message))
        return None
    return _notify


class TestFloodThrottle:
    def test_per_file_flood_is_capped(self):
        """2000 per-file updates must NOT produce 2000 sends."""
        calls = []
        clock = _FakeClock()
        rep = ProgressReporter(_collecting_notify(calls), "Index", clock=clock)
        total = 2000
        for i in range(1, total + 1):
            clock.advance(0.002)  # 2ms/file ≈ the observed real pace
            rep.update(i, total, f"file_{i}.ts")
        # interval floor (100ms) at 2ms/file → ~40 sends; assert well under
        # the old 1-per-file behavior and that the final 100% went out.
        assert len(calls) < 120, f"flood not throttled: {len(calls)} sends"
        assert calls[-1][0] == 1.0

    def test_min_step_gates_sends(self):
        """Without clock pressure, sends are capped by the 1% step."""
        calls = []
        clock = _FakeClock()
        rep = ProgressReporter(
            _collecting_notify(calls), "Index",
            min_interval=0.0, clock=clock,
        )
        total = 1000
        for i in range(1, total + 1):
            rep.update(i, total)
        # 1% step over 1000 items → ~100 sends + final
        assert len(calls) <= int(1 / PROGRESS_MIN_STEP) + 1
        assert calls[-1][0] == 1.0

    def test_final_send_bypasses_throttle(self):
        """The 100% update goes out even inside the min-interval window."""
        calls = []
        clock = _FakeClock()
        rep = ProgressReporter(_collecting_notify(calls), "Index", clock=clock)
        rep.update(50, 100)          # sends (first real step)
        rep.update(100, 100)         # final — same clock instant, must send
        assert [p for p, _ in calls] == [0.5, 1.0]

    def test_monotonic_preserved(self):
        calls = []
        clock = _FakeClock()
        rep = ProgressReporter(
            _collecting_notify(calls), "Index",
            min_interval=0.0, min_step=0.0, clock=clock,
        )
        rep.update(50, 100)
        rep.update(40, 100)  # backwards — must not send
        assert [p for p, _ in calls] == [0.5]

    def test_thread_safety_under_concurrent_updates(self):
        """Concurrent worker updates never raise and never exceed the cap."""
        calls = []
        lock = threading.Lock()

        def _notify(progress, total, message):
            with lock:
                calls.append(progress)
            return None

        clock = _FakeClock()
        rep = ProgressReporter(_notify, "Index", min_interval=0.0, clock=clock)
        threads = [
            threading.Thread(
                target=lambda base=b: [rep.update(base + i, 4000) for i in range(1000)]
            )
            for b in (0, 1000, 2000, 3000)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) <= int(1 / PROGRESS_MIN_STEP) + 2


class TestDrain:
    def test_close_stops_further_sends(self):
        calls = []
        rep = ProgressReporter(_collecting_notify(calls), "Index", min_interval=0.0)
        rep.update(50, 100)
        rep.close()
        rep.update(99, 100)
        rep.finish()
        assert [p for p, _ in calls] == [0.5]

    def test_close_returns_only_unfinished_futures(self):
        pending_fut = _FakeFuture(is_done=False)
        done_fut = _FakeFuture(is_done=True)
        futs = iter([done_fut, pending_fut])

        def _notify(progress, total, message):
            return next(futs)

        rep = ProgressReporter(_notify, "Index", min_interval=0.0)
        rep.update(50, 100)
        rep.update(100, 100)
        pending = rep.close()
        assert pending == [pending_fut]

    async def test_drain_reporter_waits_for_inflight_send(self):
        """drain_reporter awaits a real run_coroutine_threadsafe send."""
        delivered = []
        loop = asyncio.get_running_loop()

        async def _slow_send(progress):
            await asyncio.sleep(0.05)
            delivered.append(progress)

        def _notify(progress, total, message):
            return asyncio.run_coroutine_threadsafe(_slow_send(progress), loop)

        rep = ProgressReporter(_notify, "Index", min_interval=0.0)
        # schedule from a worker thread, like asyncio.to_thread does
        await asyncio.to_thread(rep.update, 100, 100)
        assert delivered == []  # still in flight
        await drain_reporter(rep)
        assert delivered == [1.0]

    async def test_drain_reporter_noop_without_pending(self):
        rep = ProgressReporter(_collecting_notify([]), "Index")
        await drain_reporter(rep)  # must not raise or hang


class TestNotifyReturnsFuture:
    async def test_make_progress_notify_returns_schedulable_future(self):
        class _Session:
            def __init__(self):
                self.sent = []

            async def send_progress_notification(self, **kw):
                self.sent.append(kw)

        class _Meta:
            progressToken = "tok-1"

        class _Ctx:
            meta = _Meta()

        session = _Session()
        _Ctx.session = session

        class _Server:
            request_context = _Ctx()

        notify = make_progress_notify(_Server())
        assert notify is not None
        fut = await asyncio.to_thread(notify, 0.5, 1.0, "halfway")
        assert fut is not None and hasattr(fut, "done")
        await asyncio.wrap_future(fut)
        assert session.sent[0]["progress"] == 0.5
