from __future__ import annotations
import asyncio
import os
import webbrowser
from pathlib import Path
import uvicorn

async def main():
    # In a PyInstaller one-folder build, frontend is bundled at sys._MEIPASS/frontend.
    runtime = Path.cwd() / "runtime"
    runtime.mkdir(exist_ok=True)
    pid_file = runtime / "app.pid"
    stop_file = runtime / "stop.request"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=int(os.getenv("PBDA_PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    async def watch_stop_request():
        while not task.done() and not server.should_exit:
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                server.should_exit = True
                return
            await asyncio.sleep(.25)

    stop_watcher = asyncio.create_task(watch_stop_request())
    try:
        while not server.started and not task.done():
            await asyncio.sleep(.2)
        if server.started:
            webbrowser.open(f"http://127.0.0.1:{config.port}")
        await task
    finally:
        stop_watcher.cancel()
        stop_file.unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)

if __name__ == "__main__":
    asyncio.run(main())
