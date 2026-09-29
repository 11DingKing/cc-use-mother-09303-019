"""聚合仓储：在事件存储上加载聚合、追加事件并更新快照。"""
from __future__ import annotations

import uuid
from typing import Any

from .domain.aggregates import Aggregate
from .errors import Conflict, NotFound
from .event_store import EventStore
from .identity import Principal


class AggregateRepo:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def exists(self, aggregate_type: str, aggregate_id: str) -> bool:
        return bool(
            self.store.connection.execute(
                "SELECT 1 FROM event_log WHERE aggregate = ? AND aggregate_id = ? LIMIT 1",
                (aggregate_type, aggregate_id),
            ).fetchone()
        )

    def load(self, aggregate_type: str, aggregate_id: str) -> Aggregate:
        events = self.store.events_for(aggregate_type, aggregate_id)
        return Aggregate(aggregate_id, events)

    def require(self, aggregate_type: str, aggregate_id: str) -> Aggregate:
        agg = self.load(aggregate_type, aggregate_id)
        if not agg.exists:
            raise NotFound(f"{aggregate_type} 不存在：{aggregate_id}")
        return agg

    def append(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        data: dict[str, Any],
        principal: Principal,
        *,
        expected_version: int | None = None,
        event_id: str | None = None,
    ) -> Aggregate:
        agg = self.load(aggregate_type, aggregate_id)
        if expected_version is not None and agg.version != expected_version:
            raise Conflict("聚合版本已变化，请刷新后重试")
        new_version = agg.version + 1
        self.store.append(
            event_id=event_id or uuid.uuid4().hex,
            aggregate=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            data=data,
            version=new_version,
            actor_id=principal.actor_id,
            actor_role=principal.role,
            org_id=principal.org_id,
        )
        agg.when(
            {
                "event_type": event_type,
                "data": data,
                "version": new_version,
            }
        )
        self.store.save_snapshot(aggregate_type, aggregate_id, new_version, agg.state)
        return agg

    def list_snapshots(self, aggregate_type: str) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for agg_id in self.store.snapshot_ids(aggregate_type):
            state = self.store.load_snapshot(aggregate_type, agg_id)
            if state:
                states.append(state)
        return states
