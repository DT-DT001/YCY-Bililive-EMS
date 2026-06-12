from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from bilibili_api import live


EventCallback = Callable[["LiveEvent"], Awaitable[None]]
StatusCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class LiveEvent:
    event_type: str
    username: str
    tier: str = "normal"
    value: float = 0
    message: str = ""
    raw_command: str = ""
    timestamp: float = 0

    def to_dict(self) -> dict:
        return asdict(self)


def tier_from_guard(level: int | str | None) -> str:
    try:
        value = int(level or 0)
    except (TypeError, ValueError):
        value = 0
    return {3: "captain", 2: "admiral", 1: "governor"}.get(value, "normal")


def _nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _interaction_event(data: dict[str, Any], command: str, now: float) -> LiveEvent | None:
    decoded = data.get("pb_decoded") if command == "INTERACT_WORD_V2" else data
    if not isinstance(decoded, dict):
        return None
    message_type = int(decoded.get("msg_type", 0) or 0)
    event_type = {
        1: "enter",
        2: "follow",
        3: "share",
        4: "follow",
        5: "follow",
    }.get(message_type)
    if not event_type:
        return None
    medal = decoded.get("fans_medal_info") or decoded.get("fans_medal") or {}
    return LiveEvent(
        event_type=event_type,
        username=str(decoded.get("uname", decoded.get("username", ""))),
        tier=tier_from_guard(
            decoded.get("privilege_type")
            or (medal.get("guard_level") if isinstance(medal, dict) else 0)
        ),
        value=1,
        message=str(decoded.get("spread_desc", decoded.get("spread_info", ""))),
        raw_command=command,
        timestamp=now,
    )


def normalize_message(message: dict[str, Any]) -> LiveEvent | None:
    command = str(message.get("cmd", "")).split(":")[0]
    data = message.get("data") or {}
    now = time.time()

    if command == "DANMU_MSG":
        info = message.get("info") or []
        user = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        medal = info[3] if len(info) > 3 and isinstance(info[3], list) else []
        guard = medal[10] if len(medal) > 10 else 0
        return LiveEvent(
            "danmu",
            str(user[1] if len(user) > 1 else ""),
            tier_from_guard(guard),
            1,
            str(info[1] if len(info) > 1 else ""),
            command,
            now,
        )

    if command == "LIKE_INFO_V3_CLICK":
        guard = data.get("guard_level") or _nested(data, "fans_medal", "guard_level")
        return LiveEvent(
            "like",
            str(data.get("uname", "")),
            tier_from_guard(guard),
            float(data.get("like_count", 1) or 1),
            "",
            command,
            now,
        )

    if command == "SEND_GIFT":
        unit_price = float(data.get("price", 0) or 0)
        count = float(data.get("num", 1) or 1)
        battery = float(data.get("total_coin", unit_price * count) or 0) / 1000
        return LiveEvent(
            "gift",
            str(data.get("uname", "")),
            tier_from_guard(data.get("guard_level")),
            battery,
            str(data.get("giftName", "")),
            command,
            now,
        )

    if command in ("GUARD_BUY", "USER_TOAST_MSG", "USER_TOAST_MSG_V2"):
        level = int(data.get("guard_level", data.get("unit", 0)) or 0)
        event_type = {
            3: "guard_captain",
            2: "guard_admiral",
            1: "guard_governor",
        }.get(level)
        if event_type:
            return LiveEvent(
                event_type,
                str(data.get("username", data.get("uname", ""))),
                tier_from_guard(level),
                float(data.get("num", 1) or 1),
                str(data.get("gift_name", data.get("toast_msg", ""))),
                command,
                now,
            )

    if command in ("INTERACT_WORD", "INTERACT_WORD_V2"):
        return _interaction_event(data, command, now)

    return None


class BilibiliListener:
    def __init__(self, on_event: EventCallback, on_status: StatusCallback) -> None:
        self.on_event = on_event
        self.on_status = on_status
        self.room_id = ""
        self.connected = False
        self.connecting = False
        self.error = ""
        self._client: live.LiveDanmaku | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, room_id: str) -> None:
        await self.stop()
        self.room_id = room_id.strip()
        if not self.room_id.isdigit():
            raise ValueError("请输入正确的数字直播间号")
        self.connecting = True
        self.connected = False
        self.error = ""
        await self.on_status(self.snapshot())
        self._client = live.LiveDanmaku(
            int(self.room_id),
            max_retry=10,
            retry_after=2,
        )
        self._register_handlers(self._client)
        self._task = asyncio.create_task(self._run())

    def _register_handlers(self, client: live.LiveDanmaku) -> None:
        @client.on("VERIFICATION_SUCCESSFUL")
        async def verification_successful(_event: dict) -> None:
            self.connected = True
            self.connecting = False
            self.error = ""
            await self.on_status(self.snapshot())

        @client.on("ALL")
        async def all_events(event: dict) -> None:
            raw = event.get("data")
            if not isinstance(raw, dict):
                return
            normalized = normalize_message(raw)
            if normalized:
                await self.on_event(normalized)

        @client.on("TIMEOUT")
        async def timeout(_event: dict) -> None:
            self.connected = False
            self.connecting = True
            self.error = "直播间心跳超时，正在自动重连"
            await self.on_status(self.snapshot())

    async def _run(self) -> None:
        try:
            assert self._client is not None
            await self._client.connect()
            if not self.connected:
                self.connecting = False
                self.error = self._client.err_reason or "直播间连接已结束"
                await self.on_status(self.snapshot())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.connected = False
            self.connecting = False
            self.error = str(exc) or type(exc).__name__
            await self.on_status(self.snapshot())

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client and client.get_status() == client.STATUS_ESTABLISHED:
            try:
                await client.disconnect()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.connected = False
        self.connecting = False
        await self.on_status(self.snapshot())

    def snapshot(self) -> dict:
        return {
            "room_id": self.room_id,
            "connected": self.connected,
            "connecting": self.connecting,
            "error": self.error,
            "engine": "bilibili-api-python",
        }
