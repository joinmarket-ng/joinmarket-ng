"""
Shared async task utilities for JoinMarket components.

Provides common patterns for periodic background tasks used by
both maker and taker components.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger


async def run_periodic_task(
    name: str,
    callback: Callable[[], Coroutine[Any, Any, None]],
    interval: float,
    initial_delay: float = 0.0,
    running_check: Callable[[], bool] | None = None,
) -> None:
    """
    Run a callback periodically until cancelled or running_check returns False.

    Args:
        name: Human-readable task name for logging
        callback: Async function to call each interval
        interval: Seconds between invocations
        initial_delay: Seconds to wait before first invocation
        running_check: Optional callable returning False to stop the task
    """
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)

    while running_check is None or running_check():
        try:
            await asyncio.sleep(interval)
            await callback()
        except asyncio.CancelledError:
            logger.info(f"{name} task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in {name}: {e}")

    logger.info(f"{name} task stopped")


def parse_directory_address(server: str, default_port: int = 5222) -> tuple[str, int]:
    """
    Parse a directory server address string into host and port.

    Args:
        server: Server address in "host:port" or "host" format
        default_port: Port to use if not specified (default: 5222)

    Returns:
        Tuple of (host, port)
    """
    parts = server.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else default_port
    return host, port


# ---------------------------------------------------------------------------
# Supervised fire-and-forget tasks
# ---------------------------------------------------------------------------
#
# ``asyncio`` only keeps a *weak* reference to running tasks: a task that no
# one references can be garbage-collected mid-flight, silently dropping the
# coroutine, and an exception it raises is reported (at best) as a cryptic
# "Task exception was never retrieved" message during interpreter shutdown.
#
# ``spawn_task`` centralizes the safe pattern for fire-and-forget work
# (notifications, commitment broadcasts, deferred resyncs, ...): it keeps a
# strong reference to the task in a module-level registry until the task
# finishes, and logs any exception the task raised, since by definition no
# caller ever awaits the result. Callers that manage task lifecycles
# explicitly (worker pools, per-client task sets) do not need this helper.

# Strong references to in-flight fire-and-forget tasks. Entries remove
# themselves on completion via the done callback.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _on_spawned_task_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.opt(exception=exc).error(f"Background task {task.get_name()!r} failed")


def spawn_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Run ``coro`` as a supervised fire-and-forget task.

    The task is kept alive (strongly referenced) until it completes and any
    exception it raises is logged instead of being silently dropped. Must be
    called from a running event loop, like ``asyncio.create_task``.

    The task is returned so callers *may* additionally await or cancel it,
    but keeping the returned handle is not required for the task to survive.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_spawned_task_done)
    return task
