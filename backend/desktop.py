from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import webview
from aiohttp import web

from .app import create_app


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


def configure_windows_app_identity() -> None:
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "YCY.Bililive.EMS"
        )


class DesktopApi:
    def save_config(self, content: str, filename: str) -> dict[str, object]:
        window = webview.windows[0]
        paths = window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=filename,
            file_types=("JSON 配置 (*.json)",),
        )
        if not paths:
            return {"saved": False}
        path = Path(paths[0])
        path.write_text(content, encoding="utf-8")
        return {"saved": True, "path": str(path)}


class DesktopServer:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runner: web.AppRunner | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self._stopping = threading.Lock()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name="ycy-local-server",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(timeout=15):
            raise RuntimeError("本地服务启动超时")
        if self.error is not None:
            raise RuntimeError("本地服务启动失败") from self.error

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start_server())
            self.ready.set()
            self.loop.run_forever()
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            if self.runner is not None:
                self.loop.run_until_complete(self.runner.cleanup())
            self.loop.close()

    async def _start_server(self) -> None:
        self.runner = web.AppRunner(create_app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 8765)
        await site.start()

    async def _close_server(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def stop(self) -> None:
        if not self._stopping.acquire(blocking=False):
            return
        try:
            if self.loop is not None and self.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self._close_server(), self.loop
                )
                try:
                    future.result(timeout=10)
                except FutureTimeoutError:
                    future.cancel()
                finally:
                    self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread is not None:
                self.thread.join(timeout=1)
        finally:
            self._stopping.release()


def fit_window_to_screen(window: webview.Window) -> None:
    time.sleep(0.2)
    screen = webview.screens[0]
    width = max(900, min(1320, screen.width - 140))
    height = max(600, min(780, screen.height - 160))
    window.resize(width, height)
    window.move(
        screen.x + 20,
        screen.y + 20,
    )


def main() -> None:
    if os.environ.get("YCY_NO_BROWSER") == "1":
        web.run_app(
            create_app(),
            host="127.0.0.1",
            port=8765,
            print=None,
        )
        return

    server = DesktopServer()
    try:
        configure_windows_app_identity()
        server.start()
        window = webview.create_window(
            "YCY Live Pulse",
            "http://127.0.0.1:8765",
            js_api=DesktopApi(),
            width=1100,
            height=680,
            min_size=(900, 600),
            background_color="#0c0714",
        )
        window.events.closed += server.stop
        webview.start(
            fit_window_to_screen,
            window,
            gui="edgechromium",
            icon=str(resource_path("logo/logo.ico")),
        )
    finally:
        server.stop()
