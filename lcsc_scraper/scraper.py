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
from .db import ExistingIndex, fetch_existing_index, load_db_config
from .company import clean_text
from .hqchip import HqChipClient
from .ickey import IcKeyClient

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# 图片过滤模式（任务配置 imageFilter）
IMAGE_FILTER_OFF = "off"          # 不过滤
IMAGE_FILTER_PLACEHOLDER = "placeholder"  # 默认：跳过「非商品实拍图」（品牌证书图 / 默认占位图）
IMAGE_FILTER_REQUIRE = "require"  # 跳过「非商品图」与「无图」，只保留有商品实拍图的商品
DEFAULT_IMAGE_FILTER = IMAGE_FILTER_PLACEHOLDER

# 「非商品实拍图」的 URL 特征。实测（data/ 8543 条样本）占 11%：
#   - 立创：https://alimg.szlcsc.com/upload/public/brand/product/certificate/…（品牌证书图，
#     860 条仅 16 张不同图，同一品牌下所有商品共用，列表页缩略图因此全部相同；
#     匹配串用 `brand/product/certificate`，不带首尾斜杠，兼容末尾无斜杠 / 其他分隔的写法）
#   - 云汉：static-ickey/assets/…/img/pic/default_list_logo.jpg（默认占位图，89 条共用 1 张）
NON_PRODUCT_IMAGE_MARKS = (
    "brand/product/certificate",
    "default_list_logo",
    "/img/pic/default",
    "placeholder",
    "no_img",
)


def is_non_product_image(url: object) -> bool:
    """图片 URL 是否为「非商品实拍图」（品牌证书图 / 默认占位图）。"""
    text = str(url or "").strip().lower()
    return any(mark in text for mark in NON_PRODUCT_IMAGE_MARKS)


def has_product_image(product: dict) -> bool:
    """商品是否有可用的商品实拍图（有 URL 且不是非商品图）。"""
    url = str(product.get("图片链接") or "").strip()
    if not url.lower().startswith("http"):
        return False
    return not is_non_product_image(url)


def passes_image_filter(product: dict, mode: str) -> bool:
    """商品图片是否满足 imageFilter 要求。

    - off：一律通过
    - placeholder（默认）：无图通过，非商品图（证书图 / 占位图）不通过
    - require：只通过有商品实拍图的商品（无图与非商品图都不通过）
    """
    if mode == IMAGE_FILTER_OFF:
        return True
    url = str(product.get("图片链接") or "").strip()
    if not url.lower().startswith("http"):
        return mode != IMAGE_FILTER_REQUIRE  # 无图：仅 require 模式跳过
    return not is_non_product_image(url)


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


def _expand_leaves(
    groups: list[dict], selected: Optional[list[dict]] = None
) -> list[tuple[str, list[str]]]:
    """把选中的分组展开为「展示名 -> [类目ID]」列表（类目 / 品牌模式共用）。

    - selected 为空时展开全部分组的全部叶子。
    - 一个展示分组可含多个类目 ID（leaf 带 childIds 时）。
    """
    if not selected:
        selected = [
            {"catalogId": g["catalogId"], "catalogName": g.get("catalogName")} for g in groups
        ]
    ordered: dict[str, list[str]] = {}
    for sel in selected:
        sid = str(sel["catalogId"])
        expanded: list[dict] = []
        for g in groups:
            if str(g["catalogId"]) == sid:
                expanded = list(g.get("leaves") or [])
                break
            leaf = next(
                (l for l in g.get("leaves") or [] if str(l["catalogId"]) == sid), None
            )
            if leaf:
                expanded = [leaf]
                break
        if not expanded:
            expanded = [{"catalogId": sid, "catalogName": sel.get("catalogName") or sid}]
        for leaf in expanded:
            name = leaf.get("group") or leaf.get("catalogName") or str(leaf["catalogId"])
            ids = leaf.get("childIds") or [str(leaf["catalogId"])]
            ordered.setdefault(name, []).extend(str(i) for i in ids)
    return list(ordered.items())


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
    supports_datasheet = True   # 列表页 fileTypeVOList 能给到数据手册 PDF 链接
    # 列表行已带最终「图片链接」「数据手册PDF链接」（enrich 不改写这两项），
    # 故可在列表阶段先做图片/数据手册过滤，省掉被拒商品的那次详情请求。
    list_image_ready = True
    list_datasheet_ready = True

    def __init__(self, concurrency: int = 6):
        self.client = LcscClient(concurrency=concurrency)
        self._cat_map: Optional[dict] = None  # catalogId -> catalogName（惰性构建）
        self._brand_info: dict[str, Optional[dict]] = {}  # 品牌ID -> 品牌页 searchResult（惰性缓存）
        self._brand_meta: dict[str, Optional[dict]] = {}  # 品牌ID -> currentBrand 官网/简介（惰性缓存）
        self._brand_lock = asyncio.Lock()
        self._brand_meta_lock = asyncio.Lock()
        self._cat_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _catalog_name(self, catalog_id) -> str:
        """类目 id -> 名称（用整棵类目树缓存，避免逐商品查父类）。"""
        if self._cat_map is None:
            async with self._cat_lock:
                if self._cat_map is None:
                    tree = await self.client.catalog_tree()
                    mapping: dict = {}

                    def walk(node: dict) -> None:
                        cid = node.get("catalogId")
                        if cid is not None:
                            mapping[cid] = node.get("catalogName")
                        for son in node.get("sonCatalogList") or []:
                            walk(son)

                    for top in tree.values():
                        walk(top)
                    self._cat_map = mapping
        return self._cat_map.get(catalog_id, "")

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

    async def category_page(self, catalog_id, page: int, brand_id=None) -> tuple[list[dict], int]:
        result = await self.client.category_products_page(
            int(catalog_id), page, brand_id=brand_id
        )
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
        # 价格：取 startPurchasedNumber 最大那档 = 最大数量档价格（对齐 scrapy01）
        max_tier = None
        for p in prices:
            s = p.get("startPurchasedNumber")
            if s is None or p.get("productPrice") is None:
                continue
            if max_tier is None or s > (max_tier.get("startPurchasedNumber") or -1):
                max_tier = p
        max_price = max_tier.get("productPrice") if max_tier else None
        # 轮播图：仅取第一张；没有就留空，不额外硬抓
        carousel = [u for u in (vo.get("luceneBreviaryImageUrls") or "").split("<$>") if u]
        first_img = carousel[0] if carousel else ""
        # 数据手册 PDF：fileTypeVOList[].detailVOList[].fileUrl
        pdf_url = ""
        for ft in vo.get("fileTypeVOList") or []:
            for d in ft.get("detailVOList") or []:
                u = d.get("fileUrl") or ""
                if u.lower().endswith(".pdf"):
                    pdf_url = "https://atta.szlcsc.com" + u
                    break
            if pdf_url:
                break
        # 包装规格：productMinEncapsulationNumber + Unit
        pack = ""
        if vo.get("productMinEncapsulationNumber") not in (None, ""):
            pack = f"{vo.get('productMinEncapsulationNumber')}{vo.get('productMinEncapsulationUnit') or ''}"
        product = {
            "商品编号": vo.get("productCode"),
            "型号": vo.get("productModel"),
            "品牌": vo.get("productGradePlateName"),
            "品牌网址": "",
            "品牌简介": "",
            "公司logo": "",
            "品牌ID": vo.get("productGradePlateId"),
            "类目": cat_name or vo.get("productType"),
            "商品描述": clean_text(vo.get("productName")),
            "封装": vo.get("encapsulationModel"),
            "库存": record.get("totalStockNumber")
            if record.get("totalStockNumber") is not None
            else vo.get("stockNumber"),
            "近期销量": vo.get("recentlySalesCount"),
            "最小起订": vo.get("minBuyNumber"),
            "包装方式": "",  # 详情页 productArrange 覆盖
            "包装规格": pack,
            "单价": max_price,
            "价格梯度": price_text,
            "毛重": "",
            "图片链接": first_img,
            "数据手册PDF链接": pdf_url,
            "关联(替代产品)型号": "",
            "详情链接": f"https://item.szlcsc.com/{vo.get('productId')}.html"
            if vo.get("productId")
            else "",
            "简介/备注": clean_text(vo.get("remark")),
        }
        # 商品参数（列表页 paramLinkedMap），详情页 paramList 会在 enrich 中覆盖/补充
        for k, v in (record.get("paramLinkedMap") or {}).items():
            if k and v not in (None, ""):
                product[f"参数:{k}"] = v
        return product

    async def enrich(self, product: dict) -> dict:
        pid = product.get("_pid")
        if not pid:
            return product
        web = await self.client.product_detail(pid)
        record = web.get("productRecord") or {}
        # 商品毛重（带 kg）
        if record.get("productWeight") is not None:
            product["毛重"] = f"{record['productWeight']} kg"
        # 包装方式：详情页 productArrange
        if record.get("productArrange"):
            product["包装方式"] = record["productArrange"]
        # 最少起订量：详情页动态值覆盖列表
        if record.get("minBuyNumber") is not None:
            product["最小起订"] = record["minBuyNumber"]
        # 品牌
        brand = web.get("brandVO") or {}
        if brand.get("brandName"):
            product["品牌"] = brand["brandName"]
            product["品牌ID"] = brand.get("brandId")
        # 品牌网址（厂商官网）+ 品牌简介：按 brandId 惰性缓存，每个品牌仅请求一次。
        # 数据源是品牌商品列表接口（client.brand_meta），不是品牌页——品牌页被腾讯验证码拦截，
        # 用它会导致这两个字段全空（见 AGENTS.md「品牌网址 / 品牌简介」一节）。
        bid = product.get("品牌ID")
        if bid not in (None, ""):
            current = await self.brand_meta(bid) or {}
            product["品牌网址"] = clean_text(current.get("companyWebsite"))
            product["品牌简介"] = clean_text(current.get("companyContext"))
            # 公司logo：同源字段 currentBrand.logoUrl（品牌 logo 图，实测多为 alimg.szlcsc.com 图床）
            logo = clean_text(current.get("logoUrl"))
            if logo:
                product["公司logo"] = logo
        # 商品参数：详情页 paramList 覆盖/补充列表页 paramLinkedMap
        for p in web.get("paramList") or []:
            name = p.get("parameterName")
            value = p.get("parameterDetailValue") or p.get("parameterValue")
            if name and value not in (None, "", "-"):
                product[f"参数:{name}"] = value
        # 类目：大类>子类（currentCatalog.parentId 查父类名）
        cc = web.get("currentCatalog") or {}
        sub_name = cc.get("catalogName")
        parent_id = cc.get("parentId")
        if sub_name and parent_id and parent_id != cc.get("catalogId"):
            try:
                parent_name = await self._catalog_name(parent_id)
            except Exception:  # noqa: BLE001
                parent_name = ""
            product["类目"] = f"{parent_name}>{sub_name}" if parent_name else sub_name
        elif sub_name:
            product["类目"] = sub_name
        # 关联(替代产品)型号
        code = product.get("商品编号") or record.get("productCode")
        if code:
            try:
                models = await self.client.substitute_models(str(code), pid)
            except Exception:  # noqa: BLE001
                models = []
            if models:
                product["关联(替代产品)型号"] = " | ".join(models)
        # 引脚图 / 焊盘图：不再抓取 —— 导出已不要这两列，而每个商品要多打一次 lceda.cn 请求。
        # 需要时用 client.pinpad_urls(商品编号) 单独取（方法保留）。
        return product

    async def brand_info(self, brand_id) -> Optional[dict]:
        """品牌页 searchResult（currentBrand 官网/简介 + catalogGroup 子类目分面）。

        按品牌缓存；请求失败或返回空（如被腾讯验证码拦截、无 __NEXT_DATA__）时缓存为 None，
        调用方需自行处理 None —— 注意 None 表示「探测失败」，不可当作「品牌无商品」。
        """
        key = str(brand_id)
        if key not in self._brand_info:
            async with self._brand_lock:
                if key not in self._brand_info:
                    try:
                        info = await self.client.brand_page_info(key)
                    except Exception:  # noqa: BLE001
                        info = None
                    if not info:
                        logger.warning("品牌页探测失败（可能被验证码拦截）: brandId=%s", key)
                        self._brand_info[key] = None
                    else:
                        self._brand_info[key] = info
        return self._brand_info[key]

    async def brand_meta(self, brand_id) -> Optional[dict]:
        """品牌元信息 currentBrand（官网 companyWebsite / 简介 companyContext），按品牌缓存。

        数据源为品牌商品列表接口（`client.brand_meta`）；探测失败时缓存 None（字段留空）。
        与 `brand_info` 分开：后者走被验证码拦截的品牌页，只有品牌模式探测子类目条数在用。
        """
        key = str(brand_id)
        if key not in self._brand_meta:
            async with self._brand_meta_lock:
                if key not in self._brand_meta:
                    try:
                        meta = await self.client.brand_meta(key)
                    except Exception:  # noqa: BLE001
                        meta = {}
                    if not meta:
                        logger.warning("品牌元信息探测失败: brandId=%s", key)
                        self._brand_meta[key] = None
                    else:
                        self._brand_meta[key] = meta
        return self._brand_meta[key]

    async def brand_scope(self, brand_id) -> dict[str, int]:
        """品牌在各子类目下的在售商品数: {子类目ID: 商品数}。

        取品牌页 catalogGroup 分面（与「类目 + 品牌」列表接口 totalCount 一致），
        用于探测该品牌实际有商品的子类目，避免对无商品的「品牌 × 子类目」组合发请求。
        探测失败抛异常，由调用方决定降级策略。
        """
        info = await self.brand_info(brand_id)
        # 必须是有效品牌页结果：为空，或既无 totalCount 也无 catalogGroup，都视为探测失败
        # （不可当作「品牌无商品」，否则会静默漏采，需降级到逐类目统计）
        if not info or ("totalCount" not in info and "catalogGroup" not in info):
            raise LcscError(f"品牌页探测失败: brandId={brand_id}")
        scope: dict[str, int] = {}
        for it in info.get("catalogGroup") or []:
            cid, count = it.get("value"), it.get("count")
            if cid is None or count is None:
                continue
            scope[str(cid)] = int(count)
        return scope

    async def brands(self) -> dict[str, list[dict]]:
        return await self.client.brand_map(DEFAULT_TOP_CATALOG_IDS)

    async def brand_leaf_counts(
        self, brand_id: int, leaves: list[tuple[str, list[str]]]
    ) -> dict[str, int]:
        """逐子类目精确统计该品牌的可采条数（品牌页不可用时的降级探测）。

        用「类目 + 品牌」列表接口的 totalCount：每个子类目一次请求，
        并发由 client 自带的并发信号量控制。返回 {子类目名: 可采条数}。
        """

        async def one(name: str, ids: list[str]) -> tuple[str, int]:
            avail = 0
            for cid in ids:
                _products, total = await self.category_page(cid, 1, brand_id)
                avail += total
            return name, avail

        pairs = await asyncio.gather(*(one(n, i) for n, i in leaves))
        return dict(pairs)

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
    supports_datasheet = False  # 站点/解析均无数据手册链接（实测 0%）

    def __init__(self, concurrency: int = 6, cookie: Optional[str] = None):
        self.client = HqChipClient(concurrency=concurrency, cookie=cookie)
        self._leaf_top: dict[str, str] = {}  # 叶子类目ID -> 顶级分组名（用于「大类>子类」）

    async def aclose(self) -> None:
        await self.client.aclose()

    async def catalog_groups(self) -> list[dict]:
        groups = await self.client.catalog_groups()
        # 记录每个叶子所属的顶级分组，供类目显示为「开关及按键>滑动开关」
        for g in groups:
            for leaf in g.get("leaves") or []:
                self._leaf_top[str(leaf["catalogId"])] = g.get("catalogName") or ""
        return groups

    async def category_page(self, catalog_id, page: int) -> tuple[list[dict], int]:
        products, total = await self.client.category_page(int(catalog_id), page)
        top = self._leaf_top.get(str(catalog_id))
        for p in products:
            m = _HQ_ITEM_ID_RE.search(p.get("详情链接") or "")
            if m:
                p["_pid"] = m.group(1)
            leaf = p.get("类目") or ""
            if top and leaf:
                p["类目"] = f"{top}>{leaf}"
        return products, total

    async def enrich(self, product: dict) -> dict:
        detail = await self.client.product_detail(product.get("详情链接") or "")
        for name, value in detail.get("params", {}).items():
            product.setdefault(f"参数:{name}", value)
        if detail.get("code"):
            product["商品编号"] = detail["code"]
        if detail.get("images"):
            product["图片链接"] = detail["images"][0]
        # 价格梯度：「1+: ¥2.21650; 30+: ¥2.13900; …」
        if detail.get("price_ladder"):
            product["价格梯度"] = detail["price_ladder"]
        # 单价取价格梯度里「数量档最大」那档（如 1000+ 档，最低价），与立创口径一致；
        # 华秋列表接口的 shop_price 常为 0（实测），故以详情页阶梯价兜底。
        if detail.get("price_bulk") is not None:
            product["单价"] = detail["price_bulk"]
        # 包装：「袋装(BAG)/10」→ 方式=袋装、规格=10
        package = detail.get("package") or ""
        if package:
            name_part, _, spec = package.partition("/")
            product["包装方式"] = name_part.split("(", 1)[0].strip()
            product["包装规格"] = spec.strip()
        # 商品描述以详情页为准（列表页可能缺），放入「简介/备注」
        if detail.get("goods_desc"):
            product["简介/备注"] = clean_text(detail["goods_desc"])
        # 品牌网址 / 品牌简介 / 公司logo：品牌页（/gongsi/{公司ID}.html），按公司ID 缓存
        company_id = detail.get("brand_company_id")
        if company_id:
            meta = await self.client.brand_meta(company_id)
            if meta.get("website"):
                product["品牌网址"] = clean_text(meta["website"])
            if meta.get("intro"):
                product["品牌简介"] = clean_text(meta["intro"])
            if meta.get("logo"):
                product["公司logo"] = clean_text(meta["logo"])
        return product

    async def brands(self) -> dict[str, list[dict]]:
        raise LcscError("华秋商城不支持品牌维度采集")

    async def brand_scope(self, brand_id) -> dict[str, int]:
        raise LcscError("华秋商城不支持品牌维度采集")

    async def brand_page(self, brand_id: int, page: int) -> tuple[list[dict], int]:
        raise LcscError("华秋商城不支持品牌维度采集")


class IcKeySite:
    """云汉芯城适配（无品牌维度、无销量排序，使用综合排序）。"""

    id = "ickey"
    label = "云汉芯城"
    supports_brand = False
    supports_datasheet = False  # 站点/解析均无数据手册链接（实测 0%）

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

    async def brand_scope(self, brand_id) -> dict[str, int]:
        raise LcscError("云汉芯城不支持品牌维度采集")

    async def brand_page(self, brand_id: int, page: int) -> tuple[list[dict], int]:
        raise LcscError("云汉芯城不支持品牌维度采集")


SITES: dict[str, dict] = {
    "lcsc": {"cls": LcscSite, "label": "立创商城"},
    "hqchip": {"cls": HqSite, "label": "华秋商城"},
    "ickey": {"cls": IcKeySite, "label": "云汉芯城"},
}


def create_site(site_id: str, concurrency: int, config: Optional[dict] = None):
    entry = SITES.get(site_id)
    if not entry:
        raise LcscError(f"未知站点: {site_id}")
    if site_id == "hqchip":
        return entry["cls"](concurrency=concurrency, cookie=(config or {}).get("cookie") or None)
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
        self.skipped_existing = 0  # 命中数据库已有商品、被跳过的条数
        self.skipped_no_image = 0  # 图片不合规（证书图 / 占位图 / 无图）、被跳过的条数
        self.skipped_no_datasheet = 0  # 无数据手册、被跳过的条数（仅支持该字段的站点计入）
        self.groups: dict[str, dict] = {}  # group名 -> {target, collected, done}
        self.current = ""
        self.logs: deque[str] = deque(maxlen=500)
        # 结果
        self.products: dict[str, list[dict]] = {}  # group名 -> [商品]
        # 采集去重：全局已见商品标识（跨子类目 / 品牌共享）
        self.seen_ids: set[str] = set()
        # 数据库 sp_goods 已有商品（商品编号 / 型号），采集时跳过
        self.existing: ExistingIndex = ExistingIndex()
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
            "skippedExisting": self.skipped_existing,
            "skippedNoImage": self.skipped_no_image,
            "skippedNoDatasheet": self.skipped_no_datasheet,
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
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
            "groups": {
                k: {"target": v.get("target", 0), "skipped": v.get("skipped", 0)}
                for k, v in self.groups.items()
            },
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
                # 恢复真实时间戳，保证「最近任务」排序正确（旧记录缺该字段时回退）
                if meta.get("createdAt"):
                    task.created_at = float(meta["createdAt"])
                task.finished_at = float(meta["finishedAt"]) if meta.get("finishedAt") else task.created_at
                for name, ginfo in (meta.get("groups") or {}).items():
                    if isinstance(ginfo, dict):
                        task.groups[name] = {
                            "target": ginfo.get("target", 0),
                            "collected": 0,
                            "skipped": ginfo.get("skipped", 0),
                            "done": True,
                        }
                    else:  # 兼容旧格式（值为 target 数字）
                        task.groups[name] = {"target": ginfo, "collected": 0, "done": True}
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
        await self._load_existing_index(task)
        site = create_site(task.site, int(task.config.get("concurrency", 6)), task.config)
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

    async def _load_existing_index(self, task: ScrapeTask) -> None:
        """读取服务器数据库 sp_goods 已有商品编号 / 型号，采集时据此跳过（不可用时跳过过滤）。"""
        if not task.config.get("skipExisting", True):
            task.log("未开启「跳过数据库已有商品」")
            return
        cfg = load_db_config(task.config.get("db") or {})
        if not cfg.get("enabled", True):
            task.log("数据库过滤已在配置中关闭")
            return
        loop = asyncio.get_running_loop()
        try:
            index = await loop.run_in_executor(None, fetch_existing_index, cfg)
        except Exception as exc:  # noqa: BLE001
            task.log(f"读取数据库异常，未启用「已有商品」过滤: {exc}")
            return
        if index.error:
            task.log(f"读取数据库失败，本次不按「已有商品」过滤：{index.error}")
        elif index:
            task.existing = index
            parts = [f"{len(index.codes)} 个商品编号"]
            if index.skus:
                parts.append(f"{len(index.skus)} 个型号")
            excl = cfg.get("excludeProductSource")
            scope = "全部 Product_Source" if excl is None else f"已排除 Product_Source={excl}"
            task.log(
                f"数据库 {cfg['database']}.{cfg['table']} 已有 "
                + "、".join(parts)
                + f"（{scope}），采集时商品编号或型号命中即跳过"
            )
        else:
            task.log(
                f"数据库 {cfg['database']}.{cfg['table']} 中暂无商品编号 / 型号，"
                f"本次不按「已有商品」过滤"
            )

    async def _run_category(self, task: ScrapeTask, site) -> None:
        groups = await site.catalog_groups()
        selected = task.config.get("catalogs") or []
        # 校验所选类目确实属于当前站点：防止「站点已切换但勾选仍是别站」把别站 catId
        # 拿去查询（会退化成数字组名、全部 0 条）。仅在成功取到类目树时校验，
        # 树为空（站点接口异常）时不误杀，保持旧行为。
        if groups and selected:
            valid = {str(g["catalogId"]) for g in groups}
            for g in groups:
                valid.update(str(l["catalogId"]) for l in g.get("leaves") or [])
            kept = [c for c in selected if str(c.get("catalogId")) in valid]
            dropped = [c for c in selected if str(c.get("catalogId")) not in valid]
            if dropped:
                names = "、".join(
                    str(c.get("catalogName") or c.get("catalogId")) for c in dropped[:10]
                )
                task.log(
                    f"忽略 {len(dropped)} 个不属于本站点({task.site})的类目：{names}"
                    + ("…" if len(dropped) > 10 else "")
                )
            selected = kept
            if not selected:
                task.log("所选类目均不属于当前站点，任务结束")
                return
        # 展开成叶子，并按「展示分组」聚合（与品牌模式共用 _expand_leaves）
        leaves: list[tuple[str, list[str]]] = _expand_leaves(groups, selected)

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
        """品牌模式：先探测每个品牌在各子类目下的在售商品数，再只采集有商品的组合。

        - 探测：每个品牌一次品牌页请求，取「品牌 × 子类目」商品数分面（catalogGroup），
          与「类目 + 品牌」列表接口的 totalCount 一致。
        - 目标：子类目目标 = Σ_品牌 min(perCount, 该品牌在该子类的商品数)，即进度目标
          就是真正会采集的条数；没有商品的组合不建分组、不发请求。
        - 分组名 = 子类目名（同一子类目跨品牌合并为一个 sheet），按商品去重。
        """
        brands = task.config.get("brands") or []
        if not brands:
            raise LcscError("未选择品牌")
        groups = await site.catalog_groups()
        # 与类目模式共用叶子展开逻辑（默认全部分组的全部子类目）
        leaves: list[tuple[str, list[str]]] = _expand_leaves(groups, [])
        per_count = int(task.config.get("perCount", 2))

        # ---- 探测：品牌 -> {子类目ID: 商品数} ----
        task.current = f"探测 {len(brands)} 个品牌的在售子类目"
        task.log(f"探测 {len(brands)} 个品牌在 {len(leaves)} 个子类目下的在售商品…")
        scopes = await asyncio.gather(
            *(site.brand_scope(int(b["brandId"])) for b in brands), return_exceptions=True
        )
        plans: dict[tuple[str, str], int] = {}  # (子类目, 品牌ID) -> 计划采集条数
        probe_failed: list[str] = []
        for done_n, (b, scope) in enumerate(zip(brands, scopes), start=1):
            bid = str(b["brandId"])
            if isinstance(scope, Exception):
                # 品牌页探测失败（如被腾讯验证码拦截）：改用「类目 + 品牌」列表接口逐类目精确统计
                task.log(
                    f"[{b['brandName']}] 品牌页探测失败，改用「类目+品牌」逐类目统计: {scope}"
                )
                counts: Optional[dict[str, int]] = None
                if hasattr(site, "brand_leaf_counts"):
                    try:
                        counts = await site.brand_leaf_counts(int(b["brandId"]), leaves)
                    except Exception as exc:  # noqa: BLE001
                        task.log(f"[{b['brandName']}] 逐类目统计失败，按全部子类目尝试: {exc}")
                        counts = None
                if counts is None:
                    probe_failed.append(b["brandName"])
                    for name, _ids in leaves:
                        plans[(name, bid)] = per_count
                else:
                    hit_n = 0
                    for name, _ids in leaves:
                        avail = counts.get(name, 0)
                        if avail > 0:
                            plans[(name, bid)] = min(per_count, avail)
                            hit_n += 1
                    task.log(f"[{b['brandName']}] 命中 {hit_n} 个子类目")
            else:
                hit_n = 0
                for name, ids in leaves:
                    avail = sum(scope.get(cid, 0) for cid in ids)
                    if avail > 0:
                        plans[(name, bid)] = min(per_count, avail)
                        hit_n += 1
                task.log(f"[{b['brandName']}] 命中 {hit_n} 个子类目")
            task.current = f"探测品牌 {done_n}/{len(brands)}"
            task._notify()
        task.current = ""

        task.total_products = sum(plans.values())
        for name, _ids in leaves:
            target = sum(plans.get((name, str(b["brandId"])), 0) for b in brands)
            if target:
                task.groups.setdefault(name, {"target": target, "collected": 0, "done": False})
        task.log(
            f"共 {len(brands)} 个品牌 × {len(leaves)} 个子类目，"
            f"命中 {len(task.groups)} 个子类目、目标 {task.total_products} 条"
            f"（每个品牌每子类最多 {per_count} 条）"
            + (f"；{len(probe_failed)} 个品牌探测失败按全类目尝试" if probe_failed else "")
        )
        if not plans:
            task.log("所选品牌在这些子类目下均无在售商品，任务结束")
            return

        sem = asyncio.Semaphore(max(1, int(task.config.get("groupParallel", 3))))

        async def run_one(b: dict, name: str, ids: list[str]) -> None:
            async with sem:
                await self._scrape_group(
                    task,
                    site,
                    group_name=name,
                    fetch_page=None,
                    per_count=plans[(name, str(b["brandId"]))],
                    catalog_ids=ids,
                    brand_id=int(b["brandId"]),
                    mark_done=False,
                )

        todo = [
            (b, name, ids)
            for b in brands
            for name, ids in leaves
            if (name, str(b["brandId"])) in plans
        ]
        results = await asyncio.gather(
            *(run_one(b, name, ids) for b, name, ids in todo), return_exceptions=True
        )
        for name in task.groups:
            task.groups[name]["done"] = True
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
        brand_id: Optional[int] = None,
        seen_ids: Optional[set[str]] = None,
        mark_done: bool = True,
    ) -> None:
        collected = 0
        skipped_here = 0  # 本组命中数据库已有商品、被跳过的条数
        skipped_img = 0  # 本组图片不合规（证书图 / 占位图 / 无图）、被跳过的条数
        skipped_ds = 0  # 本组无数据手册、被跳过的条数
        image_filter = str(task.config.get("imageFilter") or DEFAULT_IMAGE_FILTER)
        # 数据手册过滤：默认开，但只对「站点本身提供该字段」的站点生效
        # （华秋 / 云汉 无数据手册链接，若一并启用会把这两站全部跳过）
        datasheet_filter = bool(task.config.get("datasheetFilter", True)) and bool(
            getattr(site, "supports_datasheet", False)
        )
        # 去重集合：整个任务共享（同一商品在多个子类目 / 品牌下只采一次）
        if seen_ids is None:
            seen_ids = task.seen_ids
        g = task.groups.get(group_name)
        target = min(per_count, SITE_TOTAL_LIMIT)
        # 详情页并发批次大小：详情请求是耗时大头（实测立创单条 ~1.9s，10 条并发 ~2.0s），
        # 故按客户端并发额度成批并发抓取，而不是逐条 await。上限 10，避免批次过大。
        enrich_batch = max(1, min(int(task.config.get("concurrency", 6)), 10))
        exhausted = True  # 站点是否已确定没有更多商品（出错时保持 False，不下调目标）

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
                        if brand_id is not None:
                            products, total = await site.category_page(catalog_id, page, brand_id)
                        else:
                            products, total = await site.category_page(catalog_id, page)
                    else:
                        products, total = await fetch_page(page)
                except LcscError as exc:
                    task.errors += 1
                    task.log(f"[{group_name}] 第{page}页失败: {exc}")
                    exhausted = False
                    break
                if not products:
                    break
                # 阶段1：列表页即可判定的便宜去重（不发详情请求），收集候选
                # 过滤会淘汰一部分候选（如「只保留商品实拍图」无图约 44%），多取一些候选：
                # 上限 4×剩余目标，既避免为凑够条数把整页都拉详情，也避免多采。
                need = max(1, target - collected)
                cap = max(need * 4, enrich_batch)
                candidates: list[tuple[str, dict]] = []
                for product in products:
                    if len(candidates) >= cap:
                        break
                    task._check_cancel()
                    pid = str(
                        product.get("_pid") or product.get("详情链接") or product.get("型号") or ""
                    )
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    # 数据库已有商品：列表页已知商品编号 / 型号时先跳过，省一次详情请求
                    if task.existing and task.existing.matched(
                        product.get("商品编号"), product.get("型号")
                    ):
                        skipped_here += 1
                        continue
                    # 列表行已带最终图片 / 数据手册的站点（立创），先在此过滤，省一次详情请求
                    if getattr(site, "list_image_ready", False) and not passes_image_filter(
                        product, image_filter
                    ):
                        skipped_img += 1
                        continue
                    if (
                        datasheet_filter
                        and getattr(site, "list_datasheet_ready", False)
                        and not str(product.get("数据手册PDF链接") or "")
                        .strip()
                        .lower()
                        .startswith("http")
                    ):
                        skipped_ds += 1
                        continue
                    candidates.append((pid, product))
                # 阶段2：详情页并发抓取（由客户端信号量限流），再按原顺序逐条消费
                for start in range(0, len(candidates), enrich_batch):
                    if collected >= target:
                        break
                    chunk = candidates[start : start + enrich_batch]
                    task.current = f"{group_name} · 详情并发 {len(chunk)} 条"
                    results = await asyncio.gather(
                        *(site.enrich(p) for _pid, p in chunk), return_exceptions=True
                    )
                    for (pid, product), result in zip(chunk, results):
                        if collected >= target:
                            break
                        task._check_cancel()
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        if isinstance(result, Exception):
                            task.errors += 1
                            product["参数错误"] = str(result)[:200]
                            task.log(f"[{group_name}] 详情失败 {pid}: {result}")
                        else:
                            product = result
                        # 详情页才拿到商品编号 / 型号的站点，此处再核对一次
                        if task.existing and task.existing.matched(
                            product.get("商品编号"), product.get("型号")
                        ):
                            skipped_here += 1
                            continue
                        # 图片过滤：对象是详情页补全后的图片链接（华秋等站点的列表行不带图，
                        # 只在详情页才有，故统一在 enrich 之后判断，避免误杀）
                        if not passes_image_filter(product, image_filter):
                            skipped_img += 1
                            continue
                        # 数据手册过滤：无 PDF 链接的商品跳过（仅立创等提供该字段的站点）
                        if datasheet_filter and not str(
                            product.get("数据手册PDF链接") or ""
                        ).strip().lower().startswith("http"):
                            skipped_ds += 1
                            continue
                        product.pop("_pid", None)
                        task.products.setdefault(group_name, []).append(product)
                        task._append(group_name, product)
                        collected += 1
                        task.collected += 1
                        task.detail_done += 1
                        if g:
                            g["collected"] = g.get("collected", 0) + 1
                        task._notify()
                # 单类目页数用尽
                if total and page * max(1, len(products)) >= min(total, SITE_TOTAL_LIMIT):
                    if collected < target:
                        msg = f"[{group_name}] 该类目共 {min(total, SITE_TOTAL_LIMIT)} 条"
                        if skipped_here:
                            msg += f"，其中 {skipped_here} 条数据库已有（已跳过）"
                        if skipped_img:
                            msg += f"，{skipped_img} 条图片不合规（已跳过）"
                        if skipped_ds:
                            msg += f"，{skipped_ds} 条无数据手册（已跳过）"
                        msg += f"，实际采集 {collected} 条"
                        task.log(msg)
                    break
                page += 1
        # 站点确实没有更多商品（非报错/取消）时，把虚高的目标下调到实际可得条数，
        # 避免进度条永远跑不满、目标数虚高；被跳过（数据库已有 / 图片不合规 / 无数据手册）的条数同样从目标中扣除
        task.skipped_existing += skipped_here
        task.skipped_no_image += skipped_img
        task.skipped_no_datasheet += skipped_ds
        skipped_total = skipped_here + skipped_img + skipped_ds
        if g:
            # 记录本组被跳过的条数，供前端区分「无在售商品」与「已全部存在」
            g["skipped"] = g.get("skipped", 0) + skipped_total
            if exhausted:
                reduce = min(target - collected, g.get("target", 0))
            else:
                reduce = min(skipped_total, g.get("target", 0))
            if reduce > 0:
                g["target"] = g.get("target", 0) - reduce
                task.total_products = max(0, task.total_products - reduce)
        if mark_done and g:
            g["done"] = True
        msg = f"[{group_name}] 采集 {collected} 条"
        if skipped_here:
            msg += f"，跳过数据库已有 {skipped_here} 条"
        if skipped_img:
            msg += f"，跳过图片不合规 {skipped_img} 条"
        if skipped_ds:
            msg += f"，跳过无数据手册 {skipped_ds} 条"
        task.log(msg)
        task.current = ""


RUNNER = TaskRunner()
