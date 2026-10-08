"""采集任务编排：多站点（立创 / 华秋），按类目 / 按品牌，异步并发，
支持取消、进度回调与增量落盘。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .client import (
    DEFAULT_TOP_CATALOG_IDS,
    SITE_TOTAL_LIMIT,
    LcscClient,
    LcscError,
)
from .hqchip import HqChipClient
from .ickey import IcKeyClient

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"


def _node_leaves(node: dict) -> list[dict]:
    """收集一个类目节点下的全部叶子子类（含自身）。"""

    def collect(n: dict) -> list[dict]:
        sons = n.get("sonCatalogList") or []
        if not sons:
            return [{"catalogId": n["catalogId"], "catalogName": n["catalogName"]}]
        out: list[dict] = []
        for son in sons:
            out.extend(collect(son))
        return out

    return collect(node)


def _leaves_of_node(tree: dict, node_id: int) -> list[dict]:
    """在整棵类目树中找 node_id 节点，返回其全部叶子子类。

    注意：树里存在 ID 重复（如「连接器 / 端子 / 开关」与其中类「连接器」同为 365），
    因此先精确定位节点、再单独收集叶子，两步分开。
    """

    def find(node: dict) -> dict | None:
        if node.get("catalogId") == node_id:
            return node
        for son in node.get("sonCatalogList") or []:
            found = find(son)
            if found is not None:
                return found
        return None

    for top in tree.values():
        node = find(top)
        if node is not None:
            return _node_leaves(node)
    return []


# ---------------- 站点适配 ----------------


_HQ_ITEM_ID_RE = re.compile(r"item\.hqchip\.com/(\d+)\.html")


class LcscSite:
    """立创商城适配。"""

    id = "lcsc"
    label = "立创商城"
    supports_brand = True

    def __init__(self, concurrency: int = 6):
        self.client = LcscClient(concurrency=concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def catalog_groups(self) -> list[dict]:
        tree = await self.client.catalog_tree()
        groups = []
        for tid in DEFAULT_TOP_CATALOG_IDS:
            for top in tree.values():
                mids = top.get("sonCatalogList") or []
                mid = next(
                    (s for s in mids if s.get("catalogId") == tid),
                    top if top.get("catalogId") == tid else None,
                )
                if mid is None:
                    continue
                groups.append(
                    {
                        "catalogId": tid,
                        "catalogName": mid.get("catalogName"),
                        "leaves": _node_leaves(mid),
                    }
                )
                break
        return groups

    async def category_page(self, catalog_id, page: int) -> tuple[list[dict], int]:
        result = await self.client.category_products_page(int(catalog_id), page)
        search = result.get("searchResult") or {}
        total = int(result.get("totalCount") or search.get("totalCount") or 0)
        cat_name = result.get("catalogName") or ""
        products = [self._flatten(rec, cat_name) for rec in search.get("productRecordList") or []]
        for p in products:
            vo_id = (p.get("详情链接") or "").rsplit("/", 1)[-1].split(".")[0]
            if vo_id.isdigit():
                p["_pid"] = vo_id
        return products, total

    @staticmethod
    def _flatten(record: dict, cat_name: str) -> dict:
        vo = record.get("productVO") or {}
        prices = vo.get("productPriceList") or []
        price_text = "; ".join(
            f"{p.get('startPurchasedNumber')}+: ¥{p.get('productPrice')}"
            for p in prices
            if p.get("productPrice") is not None
        )
        min_price = min(
            (p.get("productPrice") for p in prices if p.get("productPrice") is not None),
            default=None,
        )
        return {
            "商品编号": vo.get("productCode"),
            "型号": vo.get("productModel"),
            "品牌": vo.get("productGradePlateName"),
            "品牌ID": vo.get("productGradePlateId"),
            "类目": cat_name or vo.get("productType"),
            "商品描述": vo.get("productName"),
            "封装": vo.get("encapsulationModel"),
            "库存": vo.get("stockNumber"),
            "近期销量": vo.get("recentlySalesCount"),
            "最小起订": vo.get("minBuyNumber"),
            "包装方式": f"{vo.get('productMinEncapsulationNumber')}{vo.get('productMinEncapsulationUnit') or ''}"
            if vo.get("productMinEncapsulationUnit")
            else "",
            "单价": min_price,
            "价格梯度": price_text,
            "毛重": "",
            "图片链接": vo.get("bigImageUrl") or vo.get("breviaryImageUrl"),
            "详情链接": f"https://item.szlcsc.com/{vo.get('productId')}.html"
            if vo.get("productId")
            else "",
            "简介/备注": vo.get("remark"),
        }

    async def enrich(self, product: dict) -> dict:
        pid = product.get("_pid")
        if not pid:
            return product
        web = await self.client.product_detail(pid)
        record = web.get("productRecord") or {}
        product["毛重"] = record.get("productWeight")
        brand = web.get("brandVO") or {}
        if brand.get("brandName"):
            product["品牌"] = brand["brandName"]
            product["品牌ID"] = brand.get("brandId")
        for p in web.get("paramList") or []:
            name = p.get("parameterName")
            value = p.get("parameterDetailValue") or p.get("parameterValue")
            if name:
                product[f"参数:{name}"] = value
        return product

    async def brands(self) -> dict[str, list[dict]]:
        return await self.client.brand_map(DEFAULT_TOP_CATALOG_IDS)

    async def brand_page(self, brand_id: int, page: int) -> tuple[list[dict], int]:
        result = await self.client.brand_products_page(brand_id, page)
        search = result.get("searchResult") or {}
        total = int(result.get("totalCount") or search.get("totalCount") or 0)
        products = [self._flatten(rec, "") for rec in search.get("productRecordList") or []]
        for p in products:
            vo_id = (p.get("详情链接") or "").rsplit("/", 1)[-1].split(".")[0]
            if vo_id.isdigit():
                p["_pid"] = vo_id
        return products, total


class HqSite:
    """华秋商城适配（无品牌维度）。"""

    id = "hqchip"
    label = "华秋商城"
    supports_brand = False

    def __init__(self, concurrency: int = 6):
        self.client = HqChipClient(concurrency=concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def catalog_groups(self) -> list[dict]:
        return await self.client.catalog_groups()

    async def category_page(self, catalog_id, page: int) -> tuple[list[dict], int]:
        products, total = await self.client.category_page(int(catalog_id), page)
        for p in products:
            m = _HQ_ITEM_ID_RE.search(p.get("详情链接") or "")
            if m:
                p["_pid"] = m.group(1)
        return products, total

    async def enrich(self, product: dict) -> dict:
        detail = await self.client.product_detail(product.get("详情链接") or "")
        for name, value in detail.get("params", {}).items():
            product.setdefault(f"参数:{name}", value)
        if detail.get("code"):
            product["商品编号"] = detail["code"]
        if detail.get("images"):
            product["图片链接"] = detail["images"][0]
        return product

    async def brands(self) -> dict[str, list[dict]]:
        raise LcscError("华秋商城不支持品牌维度采集")

    async def brand_page(self, brand_id: int, page: int) -> tuple[list[dict], int]:
        raise LcscError("华秋商城不支持品牌维度采集")


class IcKeySite:
    """云汉芯城适配（无品牌维度、无销量排序，使用综合排序）。"""

    id = "ickey"
    label = "云汉芯城"
    supports_brand = False

    def __init__(self, concurrency: int = 6):
        self.client = IcKeyClient(concurrency=concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def catalog_groups(self) -> list[dict]:
        return await self.client.catalog_groups()

    async def category_page(self, catalog_id, page: int) -> tuple[list[dict], int]:
        return await self.client.category_page(str(catalog_id), page)

    async def enrich(self, product: dict) -> dict:
        detail = await self.client.product_detail(product.get("详情链接") or "")
        for name, value in detail.get("params", {}).items():
            product.setdefault(f"参数:{name}", value)
        return product

    async def brands(self) -> dict[str, list[dict]]:
        raise LcscError("云汉芯城不支持品牌维度采集")

    async def brand_page(self, brand_id: int, page: int) -> tuple[list[dict], int]:
        raise LcscError("云汉芯城不支持品牌维度采集")


SITES: dict[str, dict] = {
    "lcsc": {"cls": LcscSite, "label": "立创商城"},
    "hqchip": {"cls": HqSite, "label": "华秋商城"},
    "ickey": {"cls": IcKeySite, "label": "云汉芯城"},
}


def create_site(site_id: str, concurrency: int):
    entry = SITES.get(site_id)
    if not entry:
        raise LcscError(f"未知站点: {site_id}")
    return entry["cls"](concurrency=concurrency)


# ---------------- 任务 ----------------


class ScrapeTask:
    """一个采集任务：进度、日志、结果、取消。"""

    def __init__(self, site: str, mode: str, config: dict):
        self.id = uuid.uuid4().hex[:12]
        self.site = site
        self.mode = mode  # 'category' | 'brand'
        self.config = config
        self.status = "pending"  # pending/running/done/error/cancelled
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        # 进度
        self.total_products = 0
        self.collected = 0
        self.detail_done = 0
        self.errors = 0
        self.groups: dict[str, dict] = {}  # group名 -> {target, collected, done}
        self.current = ""
        self.logs: deque[str] = deque(maxlen=500)
        # 结果
        self.products: dict[str, list[dict]] = {}  # group名 -> [商品]
        self._jsonl_path: Optional[Path] = None
        self._jsonl = None
        self.cancel_event = asyncio.Event()
        self._notifiers: list[asyncio.Queue] = []
        self.excel_path: Optional[Path] = None

    # ---- 通知 ----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._notifiers.append(q)
        return q

    def _notify(self) -> None:
        for q in list(self._notifiers):
            try:
                q.put_nowait(self.snapshot())
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "site": self.site,
            "mode": self.mode,
            "status": self.status,
            "totalProducts": self.total_products,
            "collected": self.collected,
            "detailDone": self.detail_done,
            "errors": self.errors,
            "groups": self.groups,
            "current": self.current,
            "error": self.error,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
            "excelReady": bool(self.excel_path and self.excel_path.exists()),
            "excelName": self.excel_path.name if self.excel_path else None,
        }

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {msg}")
        logger.info("task %s: %s", self.id, msg)
        self._notify()

    # ---- 落盘 ----
    def _open_jsonl(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self._jsonl_path = DATA_DIR / f"task_{self.id}.jsonl"
        self._jsonl = self._jsonl_path.open("w", encoding="utf-8")
        self._save_meta()

    def _save_meta(self) -> None:
        meta = {
            "id": self.id,
            "site": self.site,
            "mode": self.mode,
            "config": self.config,
            "status": self.status,
            "groups": {k: v["target"] for k, v in self.groups.items()},
        }
        (DATA_DIR / f"task_{self.id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _append(self, group: str, product: dict) -> None:
        if self._jsonl:
            self._jsonl.write(
                json.dumps({"group": group, "product": product}, ensure_ascii=False) + "\n"
            )
            self._jsonl.flush()

    def close(self) -> None:
        if self._jsonl:
            self._jsonl.close()
            self._jsonl = None

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise asyncio.CancelledError


class TaskRunner:
    """管理所有任务并执行。"""

    def __init__(self):
        self.tasks: dict[str, ScrapeTask] = {}
        self._aio_tasks: dict[str, asyncio.Task] = {}

    def load_persisted(self) -> None:
        """启动时恢复历史任务（服务器重启后仍可查看进度、导出 Excel）。"""
        if not DATA_DIR.exists():
            return
        for meta_path in DATA_DIR.glob("task_*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                tid = meta["id"]
                if tid in self.tasks:
                    continue
                task = ScrapeTask(meta.get("site") or "lcsc", meta["mode"], meta.get("config") or {})
                task.id = tid
                task.status = meta.get("status") or "done"
                task.finished_at = task.created_at
                for name, target in (meta.get("groups") or {}).items():
                    task.groups[name] = {"target": target, "collected": 0, "done": True}
                jsonl = DATA_DIR / f"task_{tid}.jsonl"
                if jsonl.exists():
                    for line in jsonl.read_text(encoding="utf-8").splitlines():
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        group = rec.get("group") or ""
                        product = rec.get("product") or {}
                        task.products.setdefault(group, []).append(product)
                        task.collected += 1
                        if group in task.groups:
                            task.groups[group]["collected"] = len(task.products[group])
                task.total_products = sum(g["target"] for g in task.groups.values())
                self.tasks[tid] = task
            except Exception:  # noqa: BLE001
                logger.exception("恢复历史任务失败: %s", meta_path)

    def create(self, site: str, mode: str, config: dict) -> ScrapeTask:
        task = ScrapeTask(site, mode, config)
        self.tasks[task.id] = task
        self._aio_tasks[task.id] = asyncio.create_task(self._run(task))
        return task

    def stop(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status == "running":
            task.cancel_event.set()
            task.log("收到停止指令，正在取消…")
            return True
        return False

    async def _run(self, task: ScrapeTask) -> None:
        task.status = "running"
        task._open_jsonl()
        site = create_site(task.site, int(task.config.get("concurrency", 6)))
        try:
            if task.mode == "category":
                await self._run_category(task, site)
            else:
                await self._run_brand(task, site)
            task.status = "done" if not task.cancel_event.is_set() else "cancelled"
        except asyncio.CancelledError:
            task.status = "cancelled"
        except LcscError as exc:
            task.status = "error"
            task.error = str(exc)
            task.log(f"任务失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("task %s crashed", task.id)
            task.status = "error"
            task.error = repr(exc)
            task.log(f"任务异常: {exc!r}")
        finally:
            task.finished_at = time.time()
            task.current = ""
            task.close()
            await site.aclose()
            task._save_meta()
            task._notify()

    # ---------- 按类目 ----------

    async def _run_category(self, task: ScrapeTask, site) -> None:
        groups = await site.catalog_groups()
        selected = task.config.get("catalogs") or []
        if not selected:
            # 默认所有分组的全部叶子
            selected = [
                {"catalogId": g["catalogId"], "catalogName": g["catalogName"]} for g in groups
            ]
        # 展开成叶子，并按「展示分组」聚合：一个分组可含多个类目 ID。
        # 注意：类目 ID 统一用字符串（云汉的 ID 带前导零，如 0701）。
        # leaf 可带 childIds（展示为一个类目、内部展开多个三级子类）。
        ordered: dict[str, list[str]] = {}  # group 展示名 -> [类目ID]
        for sel in selected:
            sid = str(sel["catalogId"])
            expanded: list[dict] = []
            for g in groups:
                gid = str(g["catalogId"])
                if gid == sid:
                    expanded = list(g["leaves"])
                    break
                leaf = next((l for l in g["leaves"] if str(l["catalogId"]) == sid), None)
                if leaf:
                    expanded = [leaf]
                    break
            if not expanded:
                expanded = [{"catalogId": sid, "catalogName": sel.get("catalogName") or sid}]
            for leaf in expanded:
                name = leaf.get("group") or leaf.get("catalogName") or str(leaf["catalogId"])
                ids = leaf.get("childIds") or [str(leaf["catalogId"])]
                ordered.setdefault(name, []).extend(str(i) for i in ids)

        leaves: list[tuple[str, list[str]]] = list(ordered.items())

        per_count = int(task.config.get("perCount", 2))
        task.total_products = per_count * len(leaves)
        for name, _ in leaves:
            task.groups.setdefault(name, {"target": per_count, "collected": 0, "done": False})
        task.log(f"共 {len(leaves)} 个子类目，每个采集 {per_count} 条")

        sem = asyncio.Semaphore(max(1, int(task.config.get("groupParallel", 3))))

        async def run_leaf(name: str, catalog_ids: list[str]) -> None:
            async with sem:
                await self._scrape_group(
                    task,
                    site,
                    group_name=name,
                    fetch_page=None,
                    per_count=per_count,
                    catalog_ids=catalog_ids,
                )

        results = await asyncio.gather(
            *(run_leaf(name, ids) for name, ids in leaves),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                task.errors += 1
                task.log(f"子类目出错: {r}")

    # ---------- 按品牌 ----------

    async def _run_brand(self, task: ScrapeTask, site) -> None:
        brands = task.config.get("brands") or []
        if not brands:
            raise LcscError("未选择品牌")
        per_count = int(task.config.get("perCount", 2))
        task.total_products = per_count * len(brands)
        for b in brands:
            task.groups.setdefault(
                b["brandName"], {"target": per_count, "collected": 0, "done": False}
            )
        task.log(f"共 {len(brands)} 个品牌，每个采集 {per_count} 条")

        sem = asyncio.Semaphore(max(1, int(task.config.get("groupParallel", 3))))

        async def run_brand(b: dict) -> None:
            async with sem:
                await self._scrape_group(
                    task,
                    site,
                    group_name=b["brandName"],
                    fetch_page=lambda p: site.brand_page(int(b["brandId"]), p),
                    per_count=per_count,
                )
        results = await asyncio.gather(*(run_brand(b) for b in brands), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                task.errors += 1
                task.log(f"品牌出错: {r}")

    # ---------- 分组抓取（一个子类目 / 一个品牌 / 一组子类目） ----------

    async def _scrape_group(
        self,
        task: ScrapeTask,
        site,
        group_name: str,
        fetch_page: Optional[Callable[[int], Any]],
        per_count: int,
        catalog_ids: Optional[list[str]] = None,
    ) -> None:
        collected = 0
        seen_ids: set[str] = set()
        target = min(per_count, SITE_TOTAL_LIMIT)

        # 一个分组可对应多个类目 ID（依次抓取，累计到 target 为止）
        id_list: list[Any] = list(catalog_ids) if catalog_ids else [None]
        for catalog_id in id_list:
            if collected >= target:
                break
            page = 1
            while collected < target and page <= 200:
                task._check_cancel()
                total = 0
                products: list[dict] = []
                task.current = (
                    f"{group_name} · 第{page}页列表"
                    if catalog_id is None
                    else f"{group_name} · {catalog_id} 第{page}页"
                )
                try:
                    if catalog_ids:
                        products, total = await site.category_page(catalog_id, page)
                    else:
                        products, total = await fetch_page(page)
                except LcscError as exc:
                    task.errors += 1
                    task.log(f"[{group_name}] 第{page}页失败: {exc}")
                    break
                if not products:
                    break
                for product in products:
                    if collected >= target:
                        break
                    task._check_cancel()
                    pid = str(
                        product.get("_pid") or product.get("详情链接") or product.get("型号") or ""
                    )
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    # 详情页（完整参数/编号/图片）
                    task.current = f"{group_name} · {product.get('型号') or pid}"
                    try:
                        product = await site.enrich(product)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        task.errors += 1
                        product["参数错误"] = str(exc)[:200]
                        task.log(f"[{group_name}] 详情失败 {pid}: {exc}")
                    product.pop("_pid", None)
                    task.products.setdefault(group_name, []).append(product)
                    task._append(group_name, product)
                    collected += 1
                    task.collected += 1
                    task.detail_done += 1
                    g = task.groups.get(group_name)
                    if g:
                        g["collected"] = collected
                    task._notify()
                    if collected >= target:
                        break
                # 单类目页数用尽
                if total and page * max(1, len(products)) >= min(total, SITE_TOTAL_LIMIT):
                    if catalog_id is None and collected < target:
                        task.log(f"[{group_name}] 站点仅有 {total} 条，不足 {target} 条")
                    break
                page += 1
        g = task.groups.get(group_name)
        if g:
            g["done"] = True
        task.log(f"[{group_name}] 完成，采集 {collected} 条")
        task.current = ""


RUNNER = TaskRunner()
