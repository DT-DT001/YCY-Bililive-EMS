from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .bilibili import BilibiliListener, LiveEvent
from .devices import DeviceManager
from .models import AppConfig, ChannelTarget, EventRule, TIERED_EVENT_TYPES
from .scheduler import ChannelOutput, Scheduler
from .waveforms import Waveform, builtin_waveforms, import_waveform


def app_data_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    path = base / "data"
    path.mkdir(exist_ok=True)
    return path


class Controller:
    def __init__(self) -> None:
        self.data_dir = app_data_dir()
        self.config_path = self.data_dir / "config.json"
        self.waveform_path = self.data_dir / "waveforms.json"
        self.config = self._load_config()
        self.waveforms = self._load_waveforms()
        self.websockets: set[web.WebSocketResponse] = set()
        self.events: list[dict] = []
        self._last_device_outputs: dict[
            tuple[str, str], tuple[float, int, int, int]
        ] = {}
        self.devices = DeviceManager(
            self.config.device_generations, self.broadcast_state
        )
        self.scheduler = Scheduler(self.waveforms, self.apply_output)
        self.listener = BilibiliListener(self.handle_live_event, self.broadcast_listener)
        self._closed = False

    def _load_config(self) -> AppConfig:
        if not self.config_path.exists():
            return AppConfig()
        try:
            return AppConfig.from_dict(json.loads(self.config_path.read_text("utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AppConfig()

    def _load_waveforms(self) -> dict[str, Waveform]:
        waveforms = builtin_waveforms()
        if self.waveform_path.exists():
            try:
                raw = json.loads(self.waveform_path.read_text("utf-8"))
                from .waveforms import WavePoint

                for item in raw:
                    waveforms[item["name"]] = Waveform(
                        item["name"],
                        [WavePoint(**point) for point in item["points"]],
                        item.get("source", "import"),
                    )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
        return waveforms

    def save(self) -> None:
        self.config.device_generations = dict(self.devices.generations)
        self.config_path.write_text(
            json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )
        imported = [
            waveform.to_dict()
            for waveform in self.waveforms.values()
            if waveform.source != "builtin"
        ]
        self.waveform_path.write_text(
            json.dumps(imported, ensure_ascii=False, indent=2), "utf-8"
        )

    async def broadcast(self, message_type: str, data: Any) -> None:
        payload = json.dumps({"type": message_type, "data": data}, ensure_ascii=False)
        dead = []
        for websocket in self.websockets:
            try:
                await websocket.send_str(payload)
            except ConnectionError:
                dead.append(websocket)
        for websocket in dead:
            self.websockets.discard(websocket)

    async def broadcast_state(self) -> None:
        await self.broadcast("devices", self.device_snapshot())

    async def broadcast_listener(self, status: dict) -> None:
        await self.broadcast("listener", status)

    async def apply_output(
        self, device_id: str, channel: str, output: ChannelOutput
    ) -> None:
        signature = (
            output.strength,
            output.mode,
            output.frequency if output.mode == 0x11 else 0,
            output.pulse_width if output.mode == 0x11 else 0,
        )
        key = (device_id, channel)
        if self._last_device_outputs.get(key) != signature:
            self._last_device_outputs[key] = signature
            try:
                await self.devices.write_output(device_id, channel, output)
            except Exception as exc:
                connection = self.devices.devices.get(device_id)
                if connection:
                    connection.state.error = str(exc)
        await self.broadcast(
            "channel_output",
            {
                "device_id": device_id,
                "channel": channel,
                "output": {
                    "strength": output.strength,
                    "remaining": output.remaining,
                    "total_remaining": output.total_remaining,
                    "waveform": output.waveform,
                    "frequency": output.frequency,
                    "pulse_width": output.pulse_width,
                    "mode": output.mode,
                    "event_name": output.event_name,
                    "queue_size": output.queue_size,
                    "history": output.history,
                    "frequency_history": output.frequency_history,
                },
            },
        )

    def matching_rules(self, event: LiveEvent) -> list[EventRule]:
        result = []
        for rule in self.config.rules:
            if not rule.enabled or rule.event_type != event.event_type:
                continue
            if rule.event_type in TIERED_EVENT_TYPES and rule.tier != event.tier:
                continue
            if rule.event_type == "danmu" and rule.keyword:
                if rule.keyword not in event.message:
                    continue
            result.append(rule)
        return result

    async def handle_live_event(self, event: LiveEvent) -> None:
        raw = event.to_dict()
        self.events.insert(0, raw)
        self.events = self.events[:200]
        await self.broadcast("live_event", raw)
        for rule in self.matching_rules(event):
            await self.scheduler.trigger(rule, event.value)

    def snapshot(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "waveforms": [waveform.to_dict() for waveform in self.waveforms.values()],
            "devices": self.device_snapshot(),
            "listener": self.listener.snapshot(),
            "events": self.events,
            "scheduler": self.scheduler.snapshot(),
        }

    def device_snapshot(self) -> list[dict]:
        devices = self.devices.snapshot()
        by_id = {device["id"]: device for device in devices}
        for scheduled in self.scheduler.snapshot():
            device = by_id.get(scheduled["device_id"])
            if not device:
                continue
            device.setdefault("outputs", {})[scheduled["channel"]] = scheduled["output"]
        return devices

    def export_event_config(self) -> dict[str, Any]:
        rules = []
        for rule in self.config.to_dict()["rules"]:
            item = dict(rule)
            item.pop("targets", None)
            rules.append(item)
        return {
            "format": "ycy-bililive-event-config",
            "version": 1,
            "rules": rules,
        }

    def import_event_config(self, payload: dict[str, Any]) -> None:
        if payload.get("format") != "ycy-bililive-event-config":
            raise ValueError("不是有效的 YCY 事件配置文件")
        if not isinstance(payload.get("rules"), list):
            raise ValueError("配置文件缺少 rules 数组")
        current_targets = {
            rule.id: [asdict(target) for target in rule.targets]
            for rule in self.config.rules
        }
        imported_rules = []
        for raw in payload["rules"]:
            if not isinstance(raw, dict) or not raw.get("id"):
                raise ValueError("配置文件包含无效的事件规则")
            item = dict(raw)
            item["targets"] = current_targets.get(str(item["id"]), [])
            imported_rules.append(item)
        imported_config = AppConfig.from_dict(
            {
                "room_id": self.config.room_id,
                "auto_connect": self.config.auto_connect,
                "device_generations": self.config.device_generations,
                "rules": imported_rules,
            }
        )
        # Reapply local output targets after parsing; exported files never contain them.
        for rule in imported_config.rules:
            rule.targets = [
                ChannelTarget(**target) for target in current_targets.get(rule.id, [])
            ]
        self.config = imported_config
        self.save()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.save()
        try:
            await asyncio.wait_for(self.scheduler.close(), timeout=2)
        except TimeoutError:
            pass
        await asyncio.gather(
            asyncio.wait_for(self.listener.stop(), timeout=3),
            asyncio.wait_for(self.devices.close(), timeout=7),
            return_exceptions=True,
        )


CONTROLLER_KEY = web.AppKey("controller", Controller)


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(request.app[CONTROLLER_KEY].snapshot())


async def save_config(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    controller.config = AppConfig.from_dict(await request.json())
    controller.devices.generations = controller.config.device_generations
    controller.save()
    await controller.broadcast("config", controller.config.to_dict())
    return web.json_response({"ok": True})


async def export_config(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    body = json.dumps(
        controller.export_event_config(), ensure_ascii=False, indent=2
    ).encode("utf-8")
    return web.Response(
        body=body,
        content_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="ycy-event-config.json"'
        },
    )


async def import_config(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    try:
        controller.import_event_config(await request.json())
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    await controller.broadcast("config", controller.config.to_dict())
    return web.json_response({"ok": True, "config": controller.config.to_dict()})


async def scan_devices(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    await controller.devices.scan()
    return web.json_response(controller.device_snapshot())


async def device_action(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    device_id = request.match_info["device_id"]
    action = request.match_info["action"]
    if action == "connect":
        await controller.devices.connect(device_id)
    elif action == "disconnect":
        await controller.devices.disconnect(device_id)
    else:
        raise web.HTTPBadRequest(text="unknown action")
    return web.json_response({"ok": True})


async def device_generation(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    device_id = request.match_info["device_id"]
    raw = await request.json()
    await controller.devices.set_generation(device_id, int(raw["generation"]))
    controller.save()
    return web.json_response({"ok": True})


async def listener_action(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    action = request.match_info["action"]
    if action == "start":
        raw = await request.json()
        controller.config.room_id = str(raw["room_id"])
        controller.save()
        await controller.listener.start(controller.config.room_id)
    elif action == "stop":
        await controller.scheduler.stop_all()
        await controller.listener.stop()
        await controller.scheduler.stop_all()
        controller.events.clear()
        await controller.broadcast("events", [])
    else:
        raise web.HTTPBadRequest(text="unknown action")
    return web.json_response({"ok": True})


async def emergency_stop(request: web.Request) -> web.Response:
    await request.app[CONTROLLER_KEY].scheduler.stop_all()
    return web.json_response({"ok": True})


async def exit_application(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]

    async def shutdown() -> None:
        await asyncio.sleep(0.15)
        await controller.close()
        os._exit(0)

    asyncio.create_task(shutdown())
    return web.json_response({"ok": True})


async def simulate_event(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    raw = await request.json()
    selected_rule = next(
        (
            rule
            for rule in controller.config.rules
            if rule.id == str(raw.get("rule_id", ""))
        ),
        None,
    )
    event = LiveEvent(
        event_type=selected_rule.event_type if selected_rule else raw["event_type"],
        username=raw.get("username", "测试用户"),
        tier=selected_rule.tier if selected_rule else raw.get("tier", "normal"),
        value=float(raw.get("value", 1)),
        message=(
            selected_rule.keyword
            if selected_rule and selected_rule.event_type == "danmu"
            else raw.get("message", "")
        ),
        raw_command="SIMULATE",
        timestamp=time.time(),
    )
    if selected_rule:
        event_raw = event.to_dict()
        controller.events.insert(0, event_raw)
        controller.events = controller.events[:200]
        await controller.broadcast("live_event", event_raw)
        await controller.scheduler.trigger(selected_rule, event.value)
    else:
        await controller.handle_live_event(event)
    return web.json_response({"ok": True})


async def upload_waveform(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.filename is None:
        raise web.HTTPBadRequest(text="请选择文件")
    suffix = Path(part.filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        while chunk := await part.read_chunk():
            handle.write(chunk)
        temp_path = Path(handle.name)
    try:
        waveform = import_waveform(temp_path)
        waveform.name = Path(part.filename).stem
        controller.waveforms[waveform.name] = waveform
        controller.scheduler.waveforms = controller.waveforms
        controller.save()
        await controller.broadcast("waveforms", [item.to_dict() for item in controller.waveforms.values()])
        return web.json_response(waveform.to_dict())
    finally:
        temp_path.unlink(missing_ok=True)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    controller = request.app[CONTROLLER_KEY]
    websocket = web.WebSocketResponse(heartbeat=30)
    await websocket.prepare(request)
    controller.websockets.add(websocket)
    await websocket.send_json(
        {"type": "snapshot", "data": controller.snapshot()}, dumps=lambda value: json.dumps(value, ensure_ascii=False)
    )
    async for message in websocket:
        if message.type == WSMsgType.ERROR:
            break
    controller.websockets.discard(websocket)
    return websocket


async def on_cleanup(app: web.Application) -> None:
    await app[CONTROLLER_KEY].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app[CONTROLLER_KEY] = Controller()
    app.router.add_get("/api/state", get_state)
    app.router.add_put("/api/config", save_config)
    app.router.add_get("/api/config/export", export_config)
    app.router.add_post("/api/config/import", import_config)
    app.router.add_post("/api/devices/scan", scan_devices)
    app.router.add_post("/api/devices/{device_id}/{action}", device_action)
    app.router.add_put("/api/devices/{device_id}/generation", device_generation)
    app.router.add_post("/api/listener/{action}", listener_action)
    app.router.add_post("/api/emergency-stop", emergency_stop)
    app.router.add_post("/api/exit", exit_application)
    app.router.add_post("/api/simulate", simulate_event)
    app.router.add_post("/api/waveforms/import", upload_waveform)
    app.router.add_get("/ws", websocket_handler)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_get("/", lambda _request: web.FileResponse(static_dir / "index.html"))
        app.router.add_static("/", static_dir, show_index=False)
    app.on_cleanup.append(on_cleanup)
    return app


async def _open_browser() -> None:
    await asyncio.sleep(0.8)
    webbrowser.open("http://127.0.0.1:8765")


def main() -> None:
    loop = asyncio.new_event_loop()
    if os.environ.get("YCY_NO_BROWSER") != "1":
        loop.create_task(_open_browser())
    web.run_app(create_app(), host="127.0.0.1", port=8765, print=None, loop=loop)


if __name__ == "__main__":
    main()
