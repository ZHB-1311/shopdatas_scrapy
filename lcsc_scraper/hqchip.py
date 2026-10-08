"""华秋商城（hqchip.com）HTTP 客户端与数据适配。

接口探测结论（2026-10 验证）：
- 类目菜单:  https://www.hqchip.com/app/cn 页面内嵌（second-category-title 分组）
- 商品列表:  GET /category/detailInfo.html?catId={id}&pageNum=100&page={n}
             &orderType=3&sort=2（销量排序）&v=pc&isSpot=0...
             列表自带 attr_values（部分参数 JSON）、shop_price、detail_url、total
- 商品详情:  GET item.hqchip.com/{stockId}.html（Nuxt SSR，参数在 __NUXT__ 内联 JS：
             extName/extValue 对；编号 G数字；图片 goodsImg 数组）
- 品牌:      列表接口不支持品牌过滤，站点无品牌维度商品页（应用户要求不做品牌模式）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Optional

import httpx

from .client import USER_AGENT, LcscError

logger = logging.getLogger(__name__)

# 三大目标分组：连接器 / 开关及按键 / 线缆及配套
TARGET_GROUP_IDS = {1512, 848, 1356}

_LIST_URL = "https://www.hqchip.com/category/detailInfo.html"

_EXT_PAIR_RE = re.compile(
    r'extName="((?:[^"\\]|\\.)*)";\s*\w+\.extValue="((?:[^"\\]|\\.)*)"'
)
_GOODS_CODE_RE = re.compile(r"G\d{6,9}")
_GOODS_IMG_RE = re.compile(r"\.goodsImg=\[([^\]]+)\]")


def _js_unescape(s: str) -> str:
    s = s.replace("\\u002F", "/").replace("\\/", "/")
    try:
        return s.encode("utf-8").decode("unicode_escape").encode("latin1", "ignore").decode("utf-8", "ignore")
    except Exception:
        return s


class HqChipClient:
    """华秋商城异步客户端：类目 / 商品列表 / 商品详情。"""

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
                "Referer": "https://www.hqchip.com/",
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
                    logger.warning("华秋请求失败(%s) %s: %s", attempt, url, exc)
                    await asyncio.sleep(1.5 * attempt)
                    continue
            if resp.status_code == 200:
                await asyncio.sleep(random.uniform(*self._delay))
                return resp
            last_exc = LcscError(f"HTTP {resp.status_code}: {url}")
            await asyncio.sleep(2.0 * attempt)
        raise LcscError(f"重试{self._max_retries}次仍失败: {url} ({last_exc})")

    # ---------- 类目 ----------

    async def catalog_groups(self) -> list[dict]:
        """三大目标分组及其全部叶子子类。"""
        if self._groups_cache is not None:
            return self._groups_cache
        resp = await self._request("GET", "https://www.hqchip.com/app/cn")
        html = resp.text
        # 分组标题: <div class="second-category-title"><a href=".../app/{gid}" class="a_p"> 名称
        heads = [
            (int(m.group(1)), m.group(2).strip(), m.start())
            for m in re.finditer(
                r'class="second-category-title"><a href="https://www\.hqchip\.com/app/(\d+)"'
                r'[^>]*class="a_p">\s*([^<]+?)\s*<',
                html,
            )
        ]
        groups = []
        for i, (gid, name, pos) in enumerate(heads):
            if gid not in TARGET_GROUP_IDS:
                continue
            end = heads[i + 1][2] if i + 1 < len(heads) else len(html)
            seg = html[pos:end]
            leaves = [
                {"catalogId": int(a), "catalogName": b.strip()}
                for a, b in re.findall(
                    r'href="https://www\.hqchip\.com/app/(\d+)"[^>]*>\s*([^<]+?)\s*</a>', seg
                )
                if int(a) != gid
            ]
            groups.append({"catalogId": gid, "catalogName": name, "leaves": leaves})
        # 按固定顺序：连接器 / 开关及按键 / 线缆及配套
        order = {1512: 0, 848: 1, 1356: 2}
        groups.sort(key=lambda g: order.get(g["catalogId"], 99))
        self._groups_cache = groups
        return groups

    # ---------- 商品列表 ----------

    async def category_page(self, catalog_id: int, page: int, page_num: int = 100) -> tuple[list[dict], int]:
        """类目商品列表（销量排序），返回 (商品列表, 总数)。"""
        params = {
            "catId": catalog_id,
            "pageNum": page_num,
            "page": page,
            "isSpot": 0,
            "haveDoc": 0,
            "goodsName": "",
            "priceStart": "",
            "priceEnd": "",
            "stockNum": "",
            "v": "pc",
            "orderType": 3,
            "sort": 2,
        }
        resp = await self._request("GET", _LIST_URL, params=params)
        try:
            data = json.loads(resp.text.lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            raise LcscError(f"华秋列表返回非JSON: catalog={catalog_id} page={page}") from exc
        if not isinstance(data.get("result"), dict) or data.get("retCode") not in (0, 200):
            raise LcscError(f"华秋列表接口异常 catalog={catalog_id} page={page}: {data.get('retMsg')}")
        result = data["result"]
        total = int(result.get("total") or 0)
        cat_name = (result.get("catInfo") or {}).get("cat_name") or ""
        products = [self._normalize_goods(g, cat_name) for g in result.get("goodsList") or []]
        return products, total

    @staticmethod
    def _normalize_goods(g: dict, cat_name: str) -> dict:
        attr_values: dict = {}
        try:
            av = g.get("attr_values")
            if isinstance(av, str) and av:
                attr_values = json.loads(av)
            elif isinstance(av, dict):
                attr_values = av
        except json.JSONDecodeError:
            pass

        detail_url = g.get("detail_url") or ""
        product = {
            "商品编号": g.get("goods_sn") if re.fullmatch(r"G\d{6,9}", str(g.get("goods_sn") or "")) else "",
            "型号": g.get("goods_name"),
            "品牌": g.get("provider_name"),
            "品牌ID": g.get("brand_id"),
            "类目": cat_name,
            "商品描述": g.get("goods_desc") or g.get("goods_other_name") or "",
            "封装": g.get("encap"),
            "库存": g.get("spot_number") if g.get("spot_number") is not None else g.get("store_number"),
            "近期销量": "",
            "最小起订": g.get("min_buynum"),
            "包装方式": "",
            "单价": g.get("shop_price"),
            "价格梯度": "",
            "毛重": "",
            "图片链接": "",
            "详情链接": detail_url,
            "简介/备注": g.get("goods_brief") or "",
        }
        for name, value in attr_values.items():
            if isinstance(value, dict):
                value = value.get("attr_value")
            if name and value not in (None, ""):
                product[f"参数:{name}"] = value
        return product

    # ---------- 商品详情 ----------

    async def product_detail(self, detail_url: str) -> dict:
        """详情页解析：完整参数 + 商品编号 + 图片列表。"""
        if not detail_url:
            raise LcscError("详情链接为空")
        resp = await self._request("GET", detail_url)
        html = resp.text

        params: dict[str, str] = {}
        for raw_name, raw_value in _EXT_PAIR_RE.findall(html):
            name = _js_unescape(raw_name)
            value = _js_unescape(raw_value)
            if name and name not in params:
                params[name] = value

        code_m = _GOODS_CODE_RE.search(html)
        code = code_m.group(0) if code_m else ""

        images: list[str] = []
        img_m = _GOODS_IMG_RE.search(html)
        if img_m:
            for u in img_m.group(1).split('","'):
                u = _js_unescape(u.strip('"'))
                if u.startswith("http"):
                    images.append(u)

        return {"params": params, "code": code, "images": images}
