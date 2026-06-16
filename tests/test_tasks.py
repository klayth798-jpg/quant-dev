from collections import deque
from dataclasses import replace

import quantdev.services.tasks as tasks_module
from fastapi.testclient import TestClient
from quantdev.api import app
from quantdev.db import _postgres_script, _qmark_to_postgres
from quantdev.models import BacktestRequest
from quantdev.services.backtest import backtest_service
from quantdev.services.tasks import TaskService
from quantdev.store import new_id, store


class MemoryRedis:
    def __init__(self):
        self.items = deque()

    def ping(self):
        return True

    def lpush(self, _queue, task_id):
        self.items.appendleft(task_id)
        return len(self.items)

    def brpop(self, _queue, timeout=0):
        del timeout
        if not self.items:
            return None
        return _queue, self.items.pop()


def test_qmark_conversion_preserves_quoted_question_marks():
    sql = "SELECT '?' AS literal, value FROM sample WHERE id = ? AND note = 'it''s ?'"
    converted = _qmark_to_postgres(sql)
    assert converted == (
        "SELECT '?' AS literal, value FROM sample "
        "WHERE id = %s AND note = 'it''s ?'"
    )


def test_postgres_schema_removes_pragmas_and_preserves_float_precision():
    converted = _postgres_script(
        "PRAGMA foreign_keys = ON;\nCREATE TABLE sample (value REAL);"
    )
    assert "PRAGMA" not in converted
    assert "DOUBLE PRECISION" in converted


def test_redis_worker_executes_persisted_task(monkeypatch):
    redis = MemoryRedis()
    service = TaskService(redis)
    monkeypatch.setattr(
        tasks_module,
        "settings",
        replace(tasks_module.settings, task_backend="redis"),
    )

    queued = service.submit(
        "factor_evaluate",
        {"factor_id": "momentum_20", "forward_days": 5, "neutralize": False},
    )
    assert queued["status"] == "QUEUED"

    completed = service.run_once(timeout=0)
    assert completed["status"] == "COMPLETED"
    assert completed["attempts"] == 1
    assert completed["result"]["factor_id"] == "momentum_20"

    redis.lpush("unused", queued["task_id"])
    assert service.run_once(timeout=0) is None
    assert store.get_task_job(queued["task_id"])["attempts"] == 1


def test_worker_retries_then_marks_failed(monkeypatch):
    redis = MemoryRedis()
    service = TaskService(redis)
    monkeypatch.setattr(
        tasks_module,
        "settings",
        replace(tasks_module.settings, task_backend="redis"),
    )

    queued = service.submit("unsupported", {}, max_attempts=2)
    first = service.run_once(timeout=0)
    second = service.run_once(timeout=0)

    assert first["status"] == "QUEUED"
    assert second["status"] == "FAILED"
    assert second["attempts"] == 2
    assert "不支持的任务类型" in second["error_message"]
    assert store.get_task_job(queued["task_id"])["status"] == "FAILED"


def test_factor_api_returns_queryable_task():
    with TestClient(app) as client:
        response = client.post(
            "/api/factors/momentum_20/evaluate",
            json={"forward_days": 5, "neutralize": False},
        )
        task = response.json()
        detail = client.get("/api/tasks/{}".format(task["task_id"]))

    assert response.status_code == 202
    assert task["status"] == "COMPLETED"
    assert detail.status_code == 200
    assert detail.json()["result"]["factor_id"] == "momentum_20"


def test_backtest_retry_reuses_preallocated_run_id():
    run_id = new_id("bt")
    first = backtest_service.run(BacktestRequest(), run_id=run_id)
    second = backtest_service.run(BacktestRequest(), run_id=run_id)

    assert first["run_id"] == run_id
    assert second["run_id"] == run_id
    assert second["created_at"] == first["created_at"]
