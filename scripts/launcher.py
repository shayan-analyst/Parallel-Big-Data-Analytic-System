from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent

# Make the top-level app package importable when this script is launched directly.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# All relative workspace paths must resolve inside this project folder.
os.chdir(ROOT)

RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
PID_FILE = RUNTIME / "app.pid"
STOP_FILE = RUNTIME / "stop.request"


async def main() -> None:
    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.getenv("PBDA_PORT", "8000")),
        log_level="info",
        access_log=False,
        reload=False,
    )
    server = uvicorn.Server(config)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    task = asyncio.create_task(server.serve())

    async def watch_stop_request() -> None:
        while not task.done() and not server.should_exit:
            if STOP_FILE.exists():
                STOP_FILE.unlink(missing_ok=True)
                server.should_exit = True
                return
            await asyncio.sleep(0.25)

    stop_watcher = asyncio.create_task(watch_stop_request())
    try:
        for _ in range(120):
            if server.started:
                webbrowser.open(f"http://127.0.0.1:{config.port}")
                break
            if task.done():
                await task
                return
            await asyncio.sleep(0.25)
        await task
    finally:
        stop_watcher.cancel()
        STOP_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nParallel Big Data Analytics server stopped.")
