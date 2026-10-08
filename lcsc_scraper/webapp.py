"""FastAPI 后端：多站点（立创 / 华秋）网页界面 + 任务 REST API + SSE 实时进度。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .excel_export import export_task
from .scraper import RUNNER, SITES, create_site, has_product_image

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
    skipExisting: bool = True   # 跳过数据库 sp_goods 已有商品
    db: dict = {}               # 数据库连接覆盖项：{host,port,user,password,database,table,codeColumn,skuColumn}
    # 图片过滤：off 不过滤 / placeholder（默认）跳过非商品图 / require 只保留有商品实拍图
    imageFilter: str = Field(default="placeholder", pattern="^(off|placeholder|require)$")
    datasheetFilter: bool = True  # 跳过无数据手册（PDF 链接）的商品，仅对提供该字段的站点生效


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


def _has_image(product: dict) -> bool:
    """商品是否有可用的商品实拍图（品牌证书图 / 默认占位图不算有图）。"""
    return has_product_image(product)


@app.get("/api/tasks/{task_id}/products")
async def task_products(
    task_id: str, limit: int = 2000, offset: int = 0, onlyWithImg: bool = False
):
    """已采集（且已通过去重）的商品数据，按类目分组并给出各类目条数（支持分页）。

    只包含真正进入结果的商品：同一商品在多个类目/品牌下只出现一次（采集去重），
    且在 sp_goods 中已存在（商品编号命中 rsku_hidden，或型号命中 sku）的商品已被跳过，
    图片不合规（品牌证书图 / 占位图，require 模式下还包括无图）的商品也已被跳过，均不会出现在这里。
    onlyWithImg=True 时仅统计/返回有商品实拍图的商品（条数与分组计数同步收窄）。

    分页：把全部商品按类目顺序拉平为一条序列，返回 [offset, offset+limit) 窗口内的行；
    每个类目项始终带完整 count，rows 只包含落在本页窗口内的行。调用方按 offset 累加即可取全量。
    """
    task = RUNNER.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    fields = ["商品编号", "型号", "品牌", "封装", "库存", "单价", "类目", "图片链接", "详情链接"]
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)

    counts = []   # [(name, count)]，保持类目顺序
    flat = []     # [(name, item)]，全量扁平序列
    for name, items in task.products.items():
        sel = [it for it in items if (not onlyWithImg or _has_image(it))]
        counts.append((name, len(sel)))
        for it in sel:
            flat.append((name, it))

    total = len(flat)
    window = flat[offset:offset + limit]

    rows_by_group = {}
    for name, it in window:
        rows_by_group.setdefault(name, []).append({f: it.get(f) for f in fields})

    groups = [
        {"name": name, "count": count, "rows": rows_by_group.get(name, [])}
        for name, count in counts
    ]
    next_offset = offset + len(window)
    return {
        "total": total,
        "groupCount": sum(1 for _, c in counts if c > 0),
        "offset": offset,
        "nextOffset": next_offset,
        "hasMore": next_offset < total,
        "onlyWithImg": onlyWithImg,
        "skippedExisting": task.skipped_existing,
        "skippedNoImage": task.skipped_no_image,
        "skippedNoDatasheet": task.skipped_no_datasheet,
        "imageFilter": task.config.get("imageFilter") or "placeholder",
        "fields": fields,
        "groups": groups,
    }


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
