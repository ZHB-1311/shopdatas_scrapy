"""云汉芯城（ickey.cn）HTTP 客户端与数据适配。

接口探测结论（2026-10 验证）：
- 类目菜单:  https://www.ickey.cn/ 首页内嵌（search.ickey.cn/cate-search?cate_id=xxx 链接）
             首页给的是二级类目（如 0701 矩形连接器、1101 开关），
             三级叶子需调 get-filter-result?second_cate_id=0701 取 third_cates（如 070111）
- 商品列表:  GET search.ickey.cn/cate-search/get-search-result?page=0&page_size=50&cate_id={三级id}
             无需签名/csrf，page_size 上限 50；站点无销量排序（仅综合/库存/价格），用默认综合排序
- 商品详情:  GET www.ickey.cn/detail/{sku}/{sno}.html（SSR，完整参数表在 HTML 表格中：
             <td class="tc table-title c-666">名称</td><td class="tc table-title">值</td>）
- 品牌:      站点无品牌维度商品页（与华秋一致，不做品牌模式）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Optional

import httpx

from .client import USER_AGENT, LcscError

logger = logging.getLogger(__name__)

_LIST_URL = "https://search.ickey.cn/cate-search/get-search-result"
_FILTER_URL = "https://search.ickey.cn/cate-search/get-filter-result"
_HOME_URL = "https://www.ickey.cn/"

# 二级类目前缀 → 分组：11 = 开关/继电器，07 = 连接器
_GROUP_DEFS = [
    {"prefix": "11", "catalogId": 11, "catalogName": "开关/继电器"},
    {"prefix": "07", "catalogId": 7, "catalogName": "连接器"},
]

_SPEC_ROW_RE = re.compile(
    r'<td class="tc table-title[^"]*"\s+width="\d+">([^<]+)</td>\s*'
    r'<td class="tc table-title"[^>]*>(.*?)</td>',
    re.S,
)


def _clean(s: str) -> str:
    return (
        re.sub(r"<[^>]+>", "", s)
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .strip()
    )


class IcKeyClient:
    """云汉芯城异步客户端：类目 / 商品列表 / 商品详情。"""

    def __init__(
        self,
        concurrency: int = 6,
        request_delay: tuple[float, float] = (0.15, 0.4),
        max_retries: int = 4,
    ):
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._delay = request_delay
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.ickey.cn/",
            },
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
        )
        self._groups_cache: Optional[list[dict]] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            async with self._sem:
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    logger.warning("云汉请求失败(%s) %s: %s", attempt, url, exc)
                    await asyncio.sleep(1.5 * attempt)
                    continue
            if resp.status_code == 200:
                await asyncio.sleep(random.uniform(*self._delay))
                return resp
            last_exc = LcscError(f"HTTP {resp.status_code}: {url}")
            await asyncio.sleep(2.0 * attempt)
        raise LcscError(f"重试{self._max_retries}次仍失败: {url} ({last_exc})")

    # ---------- 类目 ----------

    async def _third_cates(self, second_id: str) -> list[dict]:
        """取二级类目下的三级叶子（无三级则返回自身）。"""
        resp = await self._request(
            "GET", _FILTER_URL, params={"second_cate_id": second_id, "cate_id": ""}
        )
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            return []
        thirds = ((data.get("result") or {}).get("third_cates")) or []
        return [
            {"catalogId": str(t["id"]), "catalogName": t["name"]}
            for t in thirds
            if t.get("id") and t.get("name")
        ]

    async def catalog_groups(self) -> list[dict]:
        """开关/继电器 与 连接器 两大分组，叶子为三级类目。"""
        if self._groups_cache is not None:
            return self._groups_cache
        resp = await self._request("GET", _HOME_URL)
        html = resp.text
        seconds: dict[str, str] = {}
        for cid, name in re.findall(
            r'href="//search\.ickey\.cn/cate-search\?cate_id=(\d+)"[^>]*>\s*([^<]+?)\s*</a>',
            html,
        ):
            name = name.strip()
            if name and cid not in seconds:
                seconds[cid] = name

        groups = []
        for gdef in _GROUP_DEFS:
            prefix = gdef["prefix"]
            sec_ids = [(cid, nm) for cid, nm in sorted(seconds.items()) if cid.startswith(prefix)]
            # 4 位为二级类目，需展开三级；6 位及以上已是叶子，直接使用
            to_expand = [(cid, nm) for cid, nm in sec_ids if len(cid) <= 4]
            direct = [(cid, nm) for cid, nm in sec_ids if len(cid) > 4]
            thirds_list = await asyncio.gather(
                *(self._third_cates(cid) for cid, _ in to_expand), return_exceptions=True
            )
            leaves: list[dict] = []
            for (cid, nm), thirds in zip(to_expand, thirds_list):
                if isinstance(thirds, Exception) or not thirds:
                    leaves.append({"catalogId": cid, "catalogName": nm})
                else:
                    # 展示为一个二级类目，内部展开其全部三级子类
                    leaves.append(
                        {
                            "catalogId": cid,
                            "catalogName": nm,
                            "childIds": [t["catalogId"] for t in thirds],
                        }
                    )
            for cid, nm in direct:
                leaves.append({"catalogId": cid, "catalogName": nm})
            groups.append(
                {"catalogId": gdef["catalogId"], "catalogName": gdef["catalogName"], "leaves": leaves}
            )
        self._groups_cache = groups
        return groups

    # ---------- 商品列表 ----------

    async def category_page(self, catalog_id: str, page: int, page_size: int = 50) -> tuple[list[dict], int]:
        """类目商品列表（综合排序），返回 (商品列表, 总数)。page 从 0 开始。"""
        params = {"page": page - 1, "page_size": page_size, "cate_id": catalog_id}
        resp = await self._request("GET", _LIST_URL, params=params)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise LcscError(f"云汉列表返回非JSON: catalog={catalog_id} page={page}") from exc
        if not data.get("success") or not isinstance(data.get("result"), dict):
            raise LcscError(f"云汉列表接口异常 catalog={catalog_id} page={page}: {data.get('msg')}")
        result = data["result"]
        total = int(result.get("total") or 0)
        products = [self._normalize(p) for p in result.get("products") or []]
        return products, total

    @staticmethod
    def _normalize(p: dict) -> dict:
        sku = str(p.get("sku") or "")
        sno = str(p.get("pro_sno") or "")
        price = p.get("calc_sale_rmb_price")
        if isinstance(price, list):
            price = min(price) if price else None
        img = p.get("img_url") or ""
        if img.startswith("//"):
            img = "https:" + img
        return {
            "商品编号": sku,
            "型号": sno or p.get("pro_name"),
            "品牌": p.get("mfr_name") or p.get("std_mfr_name"),
            "品牌ID": p.get("mfr_id"),
            "类目": p.get("cate_name"),
            "商品描述": p.get("short_desc") or p.get("pro_desc") or "",
            "封装": p.get("reference_package") or "",
            "库存": p.get("stock"),
            "近期销量": "",
            "最小起订": p.get("moq"),
            "包装方式": f"SPQ {p.get('spq')}" if p.get("spq") else "",
            "单价": price,
            "价格梯度": "",
            "毛重": "",
            "图片链接": img,
            "详情链接": f"https://www.ickey.cn/detail/{sku}/{sno}.html" if sku and sno else "",
            "简介/备注": p.get("lifecycle") or "",
            "_pid": sku,
        }

    # ---------- 商品详情 ----------

    async def product_detail(self, detail_url: str) -> dict:
        """详情页解析：完整参数表（产品属性 / 属性值）。"""
        if not detail_url:
            raise LcscError("详情链接为空")
        resp = await self._request("GET", detail_url)
        html = resp.text
        params: dict[str, str] = {}
        for name, value in _SPEC_ROW_RE.findall(html):
            name, value = _clean(name), _clean(value)
            if name and name not in params:
                params[name] = value
        return {"params": params}
