"""FastAPI 后端：多站点（立创 / 华秋）网页界面 + 任务 REST API + SSE 实时进度。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .excel_export import export_task
from .scraper import RUNNER, SITES, create_site

app = FastAPI(title="商城商品数据采集（立创 / 华秋 / 云汉）")

STATIC_DIR = Path(__file__).parent / "static"


class TaskCreate(BaseModel):
    site: str = Field(default="lcsc", pattern="^(lcsc|hqchip|ickey)$")
    mode: str = Field(pattern="^(category|brand)$")
    perCount: int = Field(default=2, ge=1, le=1500)
    concurrency: int = Field(default=6, ge=1, le=20)
    groupParallel: int = Field(default=3, ge=1, le=10)
    catalogs: list[dict] = []   # [{catalogId, catalogName}]
    brands: list[dict] = []     # [{brandId, brandName}]


@app.on_event("startup")
async def startup():
    RUNNER.load_persisted()


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/sites")
async def sites():
    return [
        {"id": sid, "name": entry["label"], "brandSupported": entry["cls"].supports_brand}
        for sid, entry in SITES.items()
    ]


@app.get("/api/catalog")
async def catalog(site: str = "lcsc"):
    """目标分组及其全部叶子子类。"""
    if site not in SITES:
        raise HTTPException(400, f"未知站点: {site}")
    client = create_site(site, concurrency=2)
    try:
        groups = await client.catalog_groups()
        return {"site": site, "groups": groups}
    finally:
        await client.aclose()


@app.get("/api/brands")
async def brands(site: str = "lcsc", catalogIds: str | None = None):
    """品牌目录（仅立创支持）。"""
    if site not in SITES:
        raise HTTPException(400, f"未知站点: {site}")
    if not SITES[site]["cls"].supports_brand:
        raise HTTPException(400, "该站点不支持品牌维度采集")
    client = create_site(site, concurrency=2)
    try:
        brand_map = await client.brands()
        total = sum(len(v) for v in brand_map.values())
        return {"brandMap": brand_map, "total": total}
    finally:
        await client.aclose()


@app.post("/api/tasks")
async def create_task(body: TaskCreate):
    if body.mode == "brand" and not SITES[body.site]["cls"].supports_brand:
        raise HTTPException(400, "该站点不支持品牌维度采集")
    config = body.model_dump()
    task = RUNNER.create(body.site, body.mode, config)
    return {"id": task.id}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    if not RUNNER.stop(task_id):
        raise HTTPException(400, "任务不存在或已结束")
    return {"ok": True}


@app.get("/api/tasks")
async def list_tasks():
    return [t.snapshot() for t in RUNNER.tasks.values()]


@app.get("/api/tasks/{task_id}")
async def task_status(task_id: str):
    task = RUNNER.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    snap = task.snapshot()
    snap["logs"] = list(task.logs)[-200:]
    return snap


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str):
    """SSE 实时进度流。"""
    task = RUNNER.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    q = task.subscribe()

    async def gen():
        # 先推一次当前状态
        yield f"data: {json.dumps(task.snapshot(), ensure_ascii=False)}\n\n"
        while True:
            try:
                snap = await asyncio.wait_for(q.get(), timeout=15)
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
                if snap.get("status") in ("done", "error", "cancelled"):
                    # 把终态连同最新快照一起推完再结束
                    for _ in range(3):
                        yield f"data: {json.dumps(task.snapshot(), ensure_ascii=False)}\n\n"
                        await asyncio.sleep(0.3)
                    return
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}/export")
async def export_excel(task_id: str):
    task = RUNNER.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status == "running" and not task.collected:
        raise HTTPException(400, "还没有采集到数据")
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(None, export_task, task)
    task.excel_path = path
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main(host: str = "127.0.0.1", port: int = 8123):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
