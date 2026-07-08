"""Tests for shared async task utilities."""

from __future__ import annotations

import asyncio

import pytest

from jmcore.tasks import _BACKGROUND_TASKS, parse_directory_address, run_periodic_task, spawn_task


class TestRunPeriodicTask:
    """Tests for run_periodic_task."""

    @pytest.mark.asyncio
    async def test_callback_is_called(self) -> None:
        """Callback should be invoked at least once."""
        call_count = 0

        async def callback() -> None:
            nonlocal call_count
            call_count += 1

        task = asyncio.create_task(
            run_periodic_task(
                name="test",
                callback=callback,
                interval=0.01,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task  # returns normally after catching CancelledError
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_initial_delay(self) -> None:
        """Callback should not fire before initial_delay elapses."""
        call_count = 0

        async def callback() -> None:
            nonlocal call_count
            call_count += 1

        task = asyncio.create_task(
            run_periodic_task(
                name="delay-test",
                callback=callback,
                interval=0.01,
                initial_delay=0.5,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_running_check_stops_task(self) -> None:
        """Task should stop when running_check returns False."""
        call_count = 0
        keep_running = True

        async def callback() -> None:
            nonlocal call_count, keep_running
            call_count += 1
            if call_count >= 3:
                keep_running = False

        await run_periodic_task(
            name="stop-test",
            callback=callback,
            interval=0.01,
            running_check=lambda: keep_running,
        )
        assert call_count >= 3

    @pytest.mark.asyncio
    async def test_exception_in_callback_does_not_crash(self) -> None:
        """Exceptions in callback should be caught; task keeps running."""
        call_count = 0
        keep_running = True

        async def callback() -> None:
            nonlocal call_count, keep_running
            call_count += 1
            if call_count == 1:
                raise ValueError("test error")
            keep_running = False

        await asyncio.wait_for(
            run_periodic_task(
                name="error-test",
                callback=callback,
                interval=0.01,
                running_check=lambda: keep_running,
            ),
            timeout=1.0,
        )

        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cancellation_is_handled(self) -> None:
        """CancelledError should break the loop gracefully and return normally."""
        task = asyncio.create_task(
            run_periodic_task(
                name="cancel-test",
                callback=self._noop,
                interval=0.01,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        # Task catches CancelledError internally and returns normally
        await task
        assert task.done()
        assert not task.cancelled()

    @pytest.mark.asyncio
    async def test_no_running_check_runs_indefinitely(self) -> None:
        """Without running_check, task runs until cancelled."""
        call_count = 0

        async def callback() -> None:
            nonlocal call_count
            call_count += 1

        task = asyncio.create_task(
            run_periodic_task(
                name="indefinite-test",
                callback=callback,
                interval=0.01,
                running_check=None,
            )
        )
        await asyncio.sleep(0.08)
        task.cancel()
        await task  # returns normally after catching CancelledError
        assert call_count >= 3

    @staticmethod
    async def _noop() -> None:
        pass


class TestSpawnTask:
    """Tests for the supervised fire-and-forget ``spawn_task`` helper."""

    @pytest.mark.asyncio
    async def test_runs_to_completion_and_returns_task(self) -> None:
        done = asyncio.Event()

        async def work() -> None:
            done.set()

        task = spawn_task(work(), name="spawn-complete-test")
        await asyncio.wait_for(done.wait(), timeout=1.0)
        await task
        assert task.done()
        assert task.get_name() == "spawn-complete-test"

    @pytest.mark.asyncio
    async def test_holds_strong_reference_until_done(self) -> None:
        """The registry must reference the task while it runs and release it after."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()

        task = spawn_task(work())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert task in _BACKGROUND_TASKS

        release.set()
        await task
        # The done callback runs via call_soon; yield to the loop once.
        await asyncio.sleep(0)
        assert task not in _BACKGROUND_TASKS

    @pytest.mark.asyncio
    async def test_exception_is_logged_not_swallowed(self) -> None:
        from loguru import logger

        records: list[str] = []
        sink_id = logger.add(records.append, level="ERROR")
        try:

            async def boom() -> None:
                raise ValueError("kaboom")

            task = spawn_task(boom(), name="spawn-error-test")
            with pytest.raises(ValueError):
                await task
            await asyncio.sleep(0)
        finally:
            logger.remove(sink_id)

        assert any("spawn-error-test" in line and "kaboom" in line for line in records)
        assert task not in _BACKGROUND_TASKS

    @pytest.mark.asyncio
    async def test_cancelled_task_is_released_without_error_log(self) -> None:
        from loguru import logger

        records: list[str] = []
        sink_id = logger.add(records.append, level="ERROR")
        try:
            task = spawn_task(asyncio.sleep(60), name="spawn-cancel-test")
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        finally:
            logger.remove(sink_id)

        assert task not in _BACKGROUND_TASKS
        assert not any("spawn-cancel-test" in line for line in records)


class TestParseDirectoryAddress:
    """Tests for parse_directory_address."""

    def test_host_and_port(self) -> None:
        host, port = parse_directory_address("example.com:1234")
        assert host == "example.com"
        assert port == 1234

    def test_host_only_uses_default_port(self) -> None:
        host, port = parse_directory_address("example.com")
        assert host == "example.com"
        assert port == 5222

    def test_custom_default_port(self) -> None:
        host, port = parse_directory_address("example.com", default_port=9999)
        assert host == "example.com"
        assert port == 9999

    def test_onion_address_with_port(self) -> None:
        addr = "abcdef1234567890.onion:5222"
        host, port = parse_directory_address(addr)
        assert host == "abcdef1234567890.onion"
        assert port == 5222

    def test_onion_address_without_port(self) -> None:
        addr = "abcdef1234567890.onion"
        host, port = parse_directory_address(addr)
        assert host == "abcdef1234567890.onion"
        assert port == 5222

    def test_localhost(self) -> None:
        host, port = parse_directory_address("localhost:8080")
        assert host == "localhost"
        assert port == 8080

    def test_ip_address_with_port(self) -> None:
        host, port = parse_directory_address("192.168.1.1:3000")
        assert host == "192.168.1.1"
        assert port == 3000
