import json
from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json, new_id, utc_now


class TaskRepository:
    """TaskRepository：task 子域数据访问。"""

    def create_task_job(
        self,
        task_type: str,
        payload: Dict[str, Any],
        max_attempts: int,
    ) -> Dict[str, Any]:
        task_id = new_id("task")
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO task_jobs
                    (task_id, task_type, status, payload_json, result_json,
                     error_message, attempts, max_attempts, worker_id,
                     created_at, queued_at, started_at, finished_at)
                VALUES (?, ?, 'QUEUED', ?, NULL, NULL, 0, ?, NULL, ?, ?, NULL, NULL)
                """,
                (
                    task_id,
                    task_type,
                    json.dumps(payload, ensure_ascii=True),
                    max_attempts,
                    now,
                    now,
                ),
            )
        return self.get_task_job(task_id)

    def get_task_job(self, task_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._decode_task_job(row) if row else None

    def list_task_jobs(
        self,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM task_jobs"
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status.upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_task_job(row) for row in rows]

    def queued_task_ids(self, limit: int = 10000) -> List[str]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id FROM task_jobs
                WHERE status = 'QUEUED'
                ORDER BY created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def claim_task_job(
        self,
        task_id: str,
        worker_id: str,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE task_jobs
                SET status = 'RUNNING',
                    attempts = attempts + 1,
                    worker_id = ?,
                    started_at = ?,
                    finished_at = NULL,
                    error_message = NULL
                WHERE task_id = ? AND status = 'QUEUED'
                """,
                (worker_id, now, task_id),
            ).rowcount
        return self.get_task_job(task_id) if updated else None

    def complete_task_job(
        self,
        task_id: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE task_jobs
                SET status = 'COMPLETED',
                    result_json = ?,
                    error_message = NULL,
                    finished_at = ?
                WHERE task_id = ? AND status = 'RUNNING'
                """,
                (
                    json.dumps(result, ensure_ascii=True),
                    utc_now(),
                    task_id,
                ),
            )
        return self.get_task_job(task_id)

    def fail_task_job(
        self,
        task_id: str,
        error_message: str,
    ) -> Optional[Dict[str, Any]]:
        found = True
        with database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT attempts, max_attempts
                FROM task_jobs
                WHERE task_id = ? AND status = 'RUNNING'
                """,
                (task_id,),
            ).fetchone()
            if not row:
                found = False
            else:
                retry = int(row["attempts"]) < int(row["max_attempts"])
                connection.execute(
                    """
                    UPDATE task_jobs
                    SET status = ?,
                        error_message = ?,
                        queued_at = ?,
                        worker_id = NULL,
                        started_at = NULL,
                        finished_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        "QUEUED" if retry else "FAILED",
                        error_message[-4000:],
                        utc_now(),
                        None if retry else utc_now(),
                        task_id,
                    ),
                )
        if not found:
            return self.get_task_job(task_id)
        return self.get_task_job(task_id)

    def requeue_stale_task_jobs(self, started_before: str) -> List[str]:
        with database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT task_id, attempts, max_attempts FROM task_jobs
                WHERE status = 'RUNNING' AND started_at < ?
                """,
                (started_before,),
            ).fetchall()
            task_ids = [
                str(row["task_id"])
                for row in rows
                if int(row["attempts"]) < int(row["max_attempts"])
            ]
            failed_ids = [
                str(row["task_id"])
                for row in rows
                if int(row["attempts"]) >= int(row["max_attempts"])
            ]
            if task_ids:
                connection.executemany(
                    """
                    UPDATE task_jobs
                    SET status = 'QUEUED',
                        worker_id = NULL,
                        started_at = NULL,
                        queued_at = ?,
                        error_message = 'Worker 超时，任务已重新入队'
                    WHERE task_id = ? AND status = 'RUNNING'
                    """,
                    [(utc_now(), task_id) for task_id in task_ids],
                )
            if failed_ids:
                connection.executemany(
                    """
                    UPDATE task_jobs
                    SET status = 'FAILED',
                        worker_id = NULL,
                        finished_at = ?,
                        error_message = 'Worker 超时且已达到最大重试次数'
                    WHERE task_id = ? AND status = 'RUNNING'
                    """,
                    [(utc_now(), task_id) for task_id in failed_ids],
                )
        return task_ids

    @staticmethod
    def _decode_task_job(row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = decode_json(item.pop("payload_json"))
        result_json = item.pop("result_json")
        item["result"] = decode_json(result_json) if result_json else None
        return item
