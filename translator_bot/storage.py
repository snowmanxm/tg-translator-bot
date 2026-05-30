from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from translator_bot.config import Settings


class MongoStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncIOMotorClient(settings.mongodb.uri)
        self.db: AsyncIOMotorDatabase = self.client.get_default_database()
        self.messages = self.db["messages"]
        self.runtime = self.db["runtime"]

    async def setup(self) -> None:
        await self.messages.create_index([("chat_id", 1), ("date", -1)])
        await self.messages.create_index([("message_id", 1), ("chat_id", 1)], unique=True)
        await self.messages.create_index("date")
        await self.runtime.create_index("key", unique=True)

    async def close(self) -> None:
        self.client.close()

    async def save_message(self, document: dict[str, Any]) -> None:
        await self.messages.update_one(
            {"chat_id": document["chat_id"], "message_id": document["message_id"]},
            {"$set": document},
            upsert=True,
        )

    async def recent_messages(
        self,
        *,
        chat_id: int | None = None,
        since: datetime | None = None,
        limit: int = 200,
        only_chinese: bool = False,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if chat_id is not None:
            query["chat_id"] = chat_id
        if since is not None:
            query["date"] = {"$gte": since}
        if only_chinese:
            query["contains_chinese"] = True
        cursor = self.messages.find(query).sort("date", -1).limit(limit)
        rows = await cursor.to_list(length=limit)
        return list(reversed(rows))

    async def last_messages_for_chat(self, chat_id: int, count: int) -> list[dict[str, Any]]:
        cursor = self.messages.find({"chat_id": chat_id}).sort("date", -1).limit(count)
        rows = await cursor.to_list(length=count)
        return list(reversed(rows))

    async def prune_old_messages(self) -> int:
        days = self.settings.storage.auto_delete_after_days
        if days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self.messages.delete_many({"date": {"$lt": cutoff}})
        return int(result.deleted_count)

    async def get_last_summary_time(self, key: str) -> datetime | None:
        row = await self.runtime.find_one({"key": key})
        value = row.get("value") if row else None
        return value if isinstance(value, datetime) else None

    async def set_last_summary_time(self, key: str, value: datetime) -> None:
        await self.runtime.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)
