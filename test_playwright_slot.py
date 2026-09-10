"""
The single global Playwright slot (analyzer/visuals.py) used to be a bare
asyncio.Semaphore(1). A task cancelled while WAITING to acquire it could
leave it permanently "locked" with no holder on CPython 3.11 (the build
Railway runs), after which every audit/search hung forever at "Queued".
Both the /api/audit orphan-cancel and the operator's Cancel button cancel
exactly such waiting tasks.

_acquire_playwright_slot / _release_playwright_slot wrap the semaphore with
wedge-recovery: a ghost lock (holder task gone/done, or held past
_PLAYWRIGHT_MAX_HOLD) is reclaimed immediately, and a hard wait ceiling is
the backstop. These tests pin that behaviour.
"""

import asyncio
import time

import pytest

from analyzer import visuals


@pytest.fixture(autouse=True)
def _fresh_slot():
    """Every test starts and ends with a clean, unlocked slot."""
    visuals._PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)
    visuals._playwright_holder = None
    visuals._playwright_acquired_at = 0.0
    yield
    visuals._PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)
    visuals._playwright_holder = None
    visuals._playwright_acquired_at = 0.0


async def test_normal_serialization_one_at_a_time():
    order = []

    async def worker(name):
        sem = await visuals._acquire_playwright_slot()
        try:
            order.append(f"{name}-in")
            await asyncio.sleep(0.05)
            order.append(f"{name}-out")
        finally:
            visuals._release_playwright_slot(sem)

    await asyncio.gather(worker("a"), worker("b"))

    # Whoever went first must have fully finished before the other started.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    ), order


async def test_ghost_lock_with_no_holder_is_reclaimed():
    # Simulate the wedge: semaphore acquired, but nobody recorded as holding
    # it (a cancelled waiter left it like this).
    await visuals._PLAYWRIGHT_SEMAPHORE.acquire()
    visuals._playwright_holder = None
    visuals._playwright_acquired_at = time.monotonic()

    sem = await asyncio.wait_for(visuals._acquire_playwright_slot(), timeout=1.0)
    try:
        assert sem is visuals._PLAYWRIGHT_SEMAPHORE
        assert sem.locked()
        assert visuals._playwright_holder is asyncio.current_task()
    finally:
        visuals._release_playwright_slot(sem)


async def test_ghost_lock_whose_holder_task_already_finished_is_reclaimed():
    async def _done_quick():
        return None

    holder = asyncio.create_task(_done_quick())
    await holder  # holder.done() is now True

    await visuals._PLAYWRIGHT_SEMAPHORE.acquire()
    visuals._playwright_holder = holder
    visuals._playwright_acquired_at = time.monotonic()

    sem = await asyncio.wait_for(visuals._acquire_playwright_slot(), timeout=1.0)
    try:
        assert sem.locked()
        assert visuals._playwright_holder is asyncio.current_task()
    finally:
        visuals._release_playwright_slot(sem)


async def test_slot_held_past_the_max_hold_is_reclaimed(monkeypatch):
    monkeypatch.setattr(visuals, "_PLAYWRIGHT_MAX_HOLD", 0.3)

    sleeper = asyncio.create_task(asyncio.sleep(10))
    try:
        await visuals._PLAYWRIGHT_SEMAPHORE.acquire()
        visuals._playwright_holder = sleeper          # still alive, not done
        visuals._playwright_acquired_at = time.monotonic() - 5  # but held way too long

        sem = await asyncio.wait_for(visuals._acquire_playwright_slot(), timeout=1.0)
        try:
            assert sem.locked()
        finally:
            visuals._release_playwright_slot(sem)
    finally:
        sleeper.cancel()


async def test_wait_ceiling_forces_a_fresh_semaphore(monkeypatch):
    # Ghost check must NOT fire (holder alive, held briefly), so the only way
    # out is the wait ceiling.
    monkeypatch.setattr(visuals, "_PLAYWRIGHT_MAX_HOLD", 0.3)  # ceiling = 0.36s

    sleeper = asyncio.create_task(asyncio.sleep(10))
    original = visuals._PLAYWRIGHT_SEMAPHORE
    try:
        await original.acquire()
        visuals._playwright_holder = sleeper
        visuals._playwright_acquired_at = time.monotonic()

        started = time.monotonic()
        sem = await asyncio.wait_for(visuals._acquire_playwright_slot(), timeout=3.0)
        try:
            assert sem is not original          # got a rebuilt one
            assert sem is visuals._PLAYWRIGHT_SEMAPHORE
            assert sem.locked()
            assert time.monotonic() - started < 2.0
        finally:
            visuals._release_playwright_slot(sem)
    finally:
        sleeper.cancel()


async def test_release_of_an_orphaned_semaphore_is_safe():
    sem_a = await visuals._acquire_playwright_slot()
    # A later caller rebuilt the global; sem_a is now an orphan.
    visuals._PLAYWRIGHT_SEMAPHORE = asyncio.BoundedSemaphore(1)

    visuals._release_playwright_slot(sem_a)   # must not raise
    visuals._release_playwright_slot(sem_a)   # double release also fine

    # The current global is still usable.
    sem_b = await asyncio.wait_for(visuals._acquire_playwright_slot(), timeout=1.0)
    visuals._release_playwright_slot(sem_b)


async def test_on_queued_fires_only_when_the_slot_is_actually_busy():
    calls = []

    # Free slot -> no queue callback.
    sem1 = await visuals._acquire_playwright_slot(on_queued=lambda: calls.append("q1"))
    assert calls == []

    # Busy slot -> queue callback fires.
    async def _second():
        return await visuals._acquire_playwright_slot(on_queued=lambda: calls.append("q2"))

    waiter = asyncio.create_task(_second())
    await asyncio.sleep(0.05)
    assert calls == ["q2"]

    visuals._release_playwright_slot(sem1)
    sem2 = await asyncio.wait_for(waiter, timeout=1.0)
    visuals._release_playwright_slot(sem2)
