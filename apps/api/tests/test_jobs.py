from typing import Any

from redis.asyncio import Redis

from receptionist_api.jobs import JobQueue


async def test_job_runs_and_is_acked(redis: Redis) -> None:
    q = JobQueue(redis, "t1")
    seen: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        seen.append(payload)

    await q.enqueue("work", {"n": 1})
    assert await q.run_one({"work": handler}, wait_secs=1)
    assert seen == [{"n": 1}]
    assert await q.length() == 0
    assert await redis.llen(q.processing) == 0


async def test_failing_job_retries_then_dead_letters(redis: Redis) -> None:
    q = JobQueue(redis, "t2", max_attempts=2)

    async def boom(payload: dict[str, Any]) -> None:
        raise RuntimeError("nope")

    await q.enqueue("work", {"n": 2})
    assert await q.run_one({"work": boom}, wait_secs=1)  # attempt 1 -> back on the queue
    assert await q.length() == 1
    assert await q.run_one({"work": boom}, wait_secs=1)  # attempt 2 -> dead
    assert await q.length() == 0
    (dead,) = await q.dead_jobs()
    assert dead.payload == {"n": 2} and dead.attempts == 2


async def test_unknown_job_name_goes_to_retry_not_lost(redis: Redis) -> None:
    q = JobQueue(redis, "t3", max_attempts=1)
    await q.enqueue("mystery", {})
    await q.run_one({}, wait_secs=1)
    assert len(await q.dead_jobs()) == 1


async def test_recover_requeues_jobs_from_a_dead_worker(redis: Redis) -> None:
    q = JobQueue(redis, "t4")
    await q.enqueue("work", {"n": 4})
    # Simulate a worker that took the job and crashed before acking.
    await redis.lmove(q.key, q.processing, "RIGHT", "LEFT")
    assert await q.length() == 0
    assert await q.recover() == 1
    assert await q.length() == 1


async def test_empty_queue_returns_false(redis: Redis) -> None:
    assert not await JobQueue(redis, "t5").run_one({}, wait_secs=1)
