import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from quantdev.config import settings
from quantdev.models import BacktestRequest, TushareSyncRequest
from quantdev.services.backtest import backtest_service
from quantdev.services.factors import factor_service
from quantdev.services.tushare_sync import tushare_sync_service
from quantdev.store import store

logger = logging.getLogger(__name__)


class TaskQueueUnavailable(RuntimeError):
    pass


class TaskService:
    def __init__(self, redis_client=None):
        self._redis_client = redis_client
        self.worker_id = "{}:{}".format(socket.gethostname(), os.getpid())

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        try:
            from redis import Redis
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise TaskQueueUnavailable(
                "缺少 Redis 客户端，请安装项目依赖 redis"
            ) from exc
        self._redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=max(settings.worker_poll_seconds + 2, 5),
        )
        return self._redis_client

    def ping(self) -> bool:
        if settings.task_backend == "inline":
            return True
        try:
            return bool(self._redis().ping())
        except Exception:
            return False

    def submit(
        self,
        task_type: str,
        payload: Dict[str, Any],
        max_attempts: Optional[int] = None,
    ) -> Dict[str, Any]:
        job = store.create_task_job(
            task_type,
            payload,
            max_attempts or settings.task_max_attempts,
        )
        if settings.task_backend == "inline":
            result = self.run_task(job["task_id"])
            while result and result["status"] == "QUEUED":
                result = self.run_task(job["task_id"])
            return result
        if settings.task_backend != "redis":
            raise ValueError(
                "未知的 QUANTDEV_TASK_BACKEND：{}".format(settings.task_backend)
            )
        self.publish(job["task_id"])
        return job

    def publish(self, task_id: str) -> None:
        try:
            self._redis().lpush(settings.task_queue_name, task_id)
        except Exception as exc:
            raise TaskQueueUnavailable(
                "Redis 任务队列不可用，任务已持久化并会在 Worker 恢复后重投"
            ) from exc

    def recover(self) -> int:
        stale_before = (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings.task_visibility_timeout_seconds)
        ).isoformat()
        store.requeue_stale_task_jobs(stale_before)
        task_ids = store.queued_task_ids()
        for task_id in task_ids:
            self.publish(task_id)
        return len(task_ids)

    def run_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        job = store.claim_task_job(task_id, self.worker_id)
        if not job:
            return None
        try:
            result = self._dispatch(job["task_type"], job["payload"])
            return store.complete_task_job(task_id, result)
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            failed = store.fail_task_job(task_id, str(exc))
            if failed and failed["status"] == "QUEUED" and settings.task_backend == "redis":
                self.publish(task_id)
            return failed

    def run_once(self, timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
        timeout = settings.worker_poll_seconds if timeout is None else timeout
        result = self._redis().brpop(settings.task_queue_name, timeout=timeout)
        if not result:
            return None
        _, task_id = result
        return self.run_task(str(task_id))

    def loop(self) -> None:
        recovered = self.recover()
        logger.info("Worker %s started; recovered %s tasks", self.worker_id, recovered)
        last_recovery = time.monotonic()
        while True:
            try:
                self.run_once()
                if time.monotonic() - last_recovery >= 60:
                    self.recover()
                    last_recovery = time.monotonic()
            except KeyboardInterrupt:
                return
            except Exception:
                logger.exception("Worker loop error")
                time.sleep(settings.worker_poll_seconds)
                try:
                    self.recover()
                    last_recovery = time.monotonic()
                except Exception:
                    logger.exception("Worker recovery failed")

    @staticmethod
    def _dispatch(task_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if task_type == "market_sync":
            request = TushareSyncRequest.model_validate(payload["request"])
            result = tushare_sync_service.run(payload["run_id"], request)
            return {
                "run_id": result["run_id"],
                "status": result["status"],
                "stats": result["stats"],
            }
        if task_type == "factor_evaluate":
            return factor_service.evaluate(
                payload["factor_id"],
                int(payload.get("forward_days", 5)),
                bool(payload.get("neutralize", False)),
            )
        if task_type == "backtest":
            result = backtest_service.run(
                BacktestRequest.model_validate(payload["request"]),
                run_id=payload.get("run_id"),
            )
            return {
                "run_id": result["run_id"],
                "status": result["status"],
            }
        raise ValueError("不支持的任务类型：{}".format(task_type))


task_service = TaskService()
