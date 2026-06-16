from datetime import datetime, timedelta, timezone

from quantdev.db import database


class LeaseRepository:
    """LeaseRepository：lease 子域数据访问。"""

    def acquire_lease(
        self, lease_key: str, owner_id: str, ttl_seconds: int
    ) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM system_leases WHERE lease_key = ?", (lease_key,)
            ).fetchone()
            if (
                existing
                and existing["owner_id"] != owner_id
                and datetime.fromisoformat(existing["expires_at"]) > now
            ):
                return False
            connection.execute(
                """
                INSERT INTO system_leases
                    (lease_key, owner_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lease_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    lease_key,
                    owner_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return True

    def renew_lease(
        self, lease_key: str, owner_id: str, ttl_seconds: int
    ) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE system_leases
                SET expires_at = ?, updated_at = ?
                WHERE lease_key = ? AND owner_id = ?
                """,
                (
                    expires_at.isoformat(),
                    now.isoformat(),
                    lease_key,
                    owner_id,
                ),
            ).rowcount
        return bool(updated)

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM system_leases WHERE lease_key = ? AND owner_id = ?",
                (lease_key, owner_id),
            )
