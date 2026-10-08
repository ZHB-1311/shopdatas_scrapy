"""立创商城 HTTP 客户端：类目树 / 商品列表 / 品牌列表 / 商品详情。

接口探测结论（2026-10 验证）：
- 商品列表:   POST https://list.szlcsc.com/category/product  (JSON body)
- 品牌商品:   GET  https://list.szlcsc.com/brand/product?brandIdFilter=...&sortNumber=6...
- 品牌目录:   GET  https://list.szlcsc.com/brand/page/catalog?catalogIds=365,13644,423,
- 类目树:     https://staticdata.szlcsc.com/common/data/resource-catalog-list-new.json
- 商品详情:   GET  https://item.szlcsc.com/{productId}.html  (__NEXT_DATA__ 内嵌)
- 列表接口 pageSize 上限 30，单个类目返回的 totalCount 封顶 1500。
- sortNumber: 6 = 按销量排序。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import subprocess
import time
import urllib.parse
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 三大目标一级类目：连接器 / 端子 / 按键开关
DEFAULT_TOP_CATALOG_IDS = [365, 13644, 423]

SORT_BY_SALES = 6
MAX_PAGE_SIZE = 30
SITE_TOTAL_LIMIT = 1500

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
_RENDER_DATA_RE = re.compile(
    r'<textarea id="renderData"[^>]*>(.*?)</textarea>', re.S
)

# 阿里云 WAF acw_sc__v2 挑战：把挑战页的真实 JS 放进 Node 里执行拿 cookie。
# cookie 约 1 小时有效；GET 请求需附加 ?alichlgref=<referrer> 才被服务端接受。
_CHALLENGE_MARKS = ("renderData", "acw_sc__v2", "_xvasu")
_ALICHLG_REF = "https://list.szlcsc.com/"

_NODE_SOLVER_TEMPLATE = """
const renderJSON = %(render)s;
let cookieJar = {};
globalThis.document = { getElementById: () => ({ innerHTML: renderJSON }), referrer: %(ref)s };
Object.defineProperty(globalThis.document, 'cookie', {
  get() { return Object.entries(cookieJar).map(([k,v])=>k+'='+v).join('; '); },
  set(v) { const [kv] = v.split(';'); const i = kv.indexOf('='); cookieJar[kv.slice(0,i).trim()] = kv.slice(i+1); }
});
globalThis.location = { href: %(href)s, reload(){}, assign(){}, replace(){} };
globalThis.navigator = { userAgent: %(ua)s };
globalThis.window = globalThis;
const timer = setInterval(() => {
  if (cookieJar['acw_sc__v2']) { console.log(JSON.stringify(cookieJar)); clearInterval(timer); process.exit(0); }
}, 50);
setTimeout(() => { console.log(JSON.stringify(cookieJar)); process.exit(0); }, 8000);
try { (0, eval)(%(s0)s); (0, eval)(%(s1)s); } catch (e) { console.error('ERR:' + e.message); }
"""


def _is_challenge(resp: httpx.Response) -> bool:
    body = resp.text[:6000]
    return any(mark in body for mark in _CHALLENGE_MARKS)


def solve_challenge_sync(html: str, url: str) -> Optional[str]:
    """执行挑战页 JS（Node 子进程），返回 acw_sc__v2 cookie 值。"""
    m = _RENDER_DATA_RE.search(html)
    if not m:
        return None
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if len(scripts) < 2:
        return None
    js = _NODE_SOLVER_TEMPLATE % {
        "render": json.dumps(m.group(1)),
        "ref": json.dumps(_ALICHLG_REF),
        "href": json.dumps(url),
        "ua": json.dumps(USER_AGENT),
        "s0": json.dumps(scripts[0]),
        "s1": json.dumps(scripts[1]),
    }
    try:
        proc = subprocess.run(
            ["node", "-e", js], capture_output=True, text=True, timeout=25
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.error("求解反爬挑战失败：需要 Node.js 环境（node 命令不可用或超时）")
        return None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            jar = json.loads(line)
        except json.JSONDecodeError:
            continue
        if jar.get("acw_sc__v2"):
            return jar["acw_sc__v2"]
    return None


# 另一种 WAF 变体（如品牌页的 `_xvasu`）：页面内联脚本直接 document.cookie=...
# 这里在 Node 里执行页面的全部内联脚本，捕获其写入的 cookie（含 tws2_* 等）。
_NODE_JAR_TEMPLATE = """
let cookieJar = {};
globalThis.document = { getElementById: () => ({ innerHTML: '' }), referrer: %(ref)s };
Object.defineProperty(globalThis.document, 'cookie', {
  get() { return Object.entries(cookieJar).map(([k,v])=>k+'='+v).join('; '); },
  set(v) { const [kv] = v.split(';'); const i = kv.indexOf('='); if (i > 0) cookieJar[kv.slice(0,i).trim()] = kv.slice(i+1); }
});
globalThis.location = { href: %(href)s, reload(){}, assign(){}, replace(){} };
globalThis.navigator = { userAgent: %(ua)s };
globalThis.window = globalThis;
const timer = setInterval(() => {
  if (Object.keys(cookieJar).length) { console.log(JSON.stringify(cookieJar)); clearInterval(timer); process.exit(0); }
}, 50);
setTimeout(() => { console.log(JSON.stringify(cookieJar)); process.exit(0); }, 8000);
%(evals)s
"""


def solve_scripts_sync(html: str, url: str) -> dict[str, str]:
    """执行页面内联脚本，返回其写入的 cookie jar（用于 _xvasu 类挑战）。"""
    scripts = [
        s for s in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) if s.strip()
    ]
    if not scripts:
        return {}
    evals = "; ".join(
        f"try{{(0,eval)({json.dumps(s)})}}catch(e){{}}" for s in scripts
    )
    js = _NODE_JAR_TEMPLATE % {
        "ref": json.dumps(_ALICHLG_REF),
        "href": json.dumps(url),
        "ua": json.dumps(USER_AGENT),
        "evals": evals,
    }
    try:
        proc = subprocess.run(
            ["node", "-e", js], capture_output=True, text=True, timeout=25
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.error("反爬挑战求解失败：需要 Node.js 环境（node 命令不可用或超时）")
        return {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            jar = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(jar, dict):
            return {k: str(v) for k, v in jar.items()}
    return {}


class LcscError(Exception):
    pass


class LcscClient:
    """异步 HTTP 客户端，带并发信号量、重试与限速。"""

    def __init__(
        self,
        concurrency: int = 6,
        request_delay: tuple[float, float] = (0.15, 0.5),
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
                "Referer": "https://list.szlcsc.com/",
            },
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
        )
        self._acw_cookie: Optional[str] = None
        self._acw_time = 0.0
        self._acw_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _sleep(self) -> None:
        lo, hi = self._delay
        await asyncio.sleep(random.uniform(lo, hi))

    def _has_fresh_acw(self) -> bool:
        return bool(self._acw_cookie) and (time.time() - self._acw_time) < 3000

    async def _solve(self, html: str, url: str) -> bool:
        """求解挑战并写入 cookie jar。

        - renderData（acw 变体）：cookie 约 1 小时有效，可复用；并发下只解一次。
        - 其它（如 `_xvasu` 脚本变体，品牌页）：值随请求变化，每次重新执行脚本取 cookie。
        """
        if _RENDER_DATA_RE.search(html):
            async with self._acw_lock:
                if self._has_fresh_acw():
                    return True
                value = await asyncio.to_thread(solve_challenge_sync, html, url)
                if not value:
                    return False
                self._acw_cookie = value
                self._acw_time = time.time()
                self._client.cookies.set("acw_sc__v2", value, domain="szlcsc.com")
                logger.info("反爬挑战已求解（acw），cookie 有效期约1小时")
                return True
        jar = await asyncio.to_thread(solve_scripts_sync, html, url)
        if not jar:
            return False
        for name, value in jar.items():
            self._client.cookies.set(name, value, domain="szlcsc.com")
        logger.info("反爬挑战已求解（脚本变体），cookie: %s", ",".join(jar))
        return True

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            async with self._sem:
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    logger.warning("请求失败(%s/%s) %s: %s", attempt, self._max_retries, url, exc)
                    await asyncio.sleep(1.5 * attempt)
                    continue
            body_head = resp.text[:6000] if resp.status_code in (200, 203) else ""
            challenged = any(mark in body_head for mark in _CHALLENGE_MARKS)
            acl_blocked = "非法ACL" in body_head
            if resp.status_code in (200, 203) and not challenged and not acl_blocked:
                await self._sleep()
                return resp
            last_exc = LcscError(
                f"HTTP {resp.status_code}{'(反爬挑战)' if challenged else '(ACL拦截)' if acl_blocked else ''}: {url}"
            )
            if challenged:
                # 解挑战：cookie + GET 附加 alichlgref 参数
                solved = await self._solve(resp.text, url)
                if solved:
                    if method.upper() == "GET" and "alichlgref" not in url:
                        sep = "&" if "?" in url else "?"
                        url = f"{url}{sep}alichlgref={urllib.parse.quote(_ALICHLG_REF, safe='')}"
                    continue
            if acl_blocked:
                # ACL 拦截通常因为缺 Referer/Origin，补齐后重试
                kwargs.setdefault("headers", {})
                kwargs["headers"].setdefault("Origin", "https://list.szlcsc.com")
                kwargs["headers"].setdefault("Referer", "https://list.szlcsc.com/")
            logger.warning("被拦截(%s/%s) %s: HTTP %s", attempt, self._max_retries, url, resp.status_code)
            await asyncio.sleep(2.0 * attempt + 1.0)
        raise LcscError(f"重试{self._max_retries}次仍失败: {url} ({last_exc})")

    # ---------- 类目 ----------

    async def catalog_tree(self) -> dict[str, Any]:
        """整棵类目树: {一级类目名: {catalogId, sonCatalogList: [...]}}"""
        resp = await self._request(
            "GET",
            "https://staticdata.szlcsc.com/common/data/resource-catalog-list-new.json",
        )
        data = resp.json()
        if data.get("code") != 200:
            raise LcscError(f"类目树接口异常: {data.get('msg')}")
        return data["result"]

    def leaves_of_top(self, tree: dict, top_catalog_id: int) -> list[dict]:
        """取某个一级类目下所有叶子类目 [{catalogId, catalogName}]"""
        for top in tree.values():
            if top.get("catalogId") == top_catalog_id:
                leaves = []
                for mid in top.get("sonCatalogList") or []:
                    if mid.get("sonCatalogList"):
                        for leaf in mid["sonCatalogList"]:
                            leaves.append(
                                {"catalogId": leaf["catalogId"], "catalogName": leaf["catalogName"]}
                            )
                    else:
                        leaves.append(
                            {"catalogId": mid["catalogId"], "catalogName": mid["catalogName"]}
                        )
                return leaves
        return []

    # ---------- 商品列表 ----------

    async def category_products_page(
        self, catalog_id: int, page: int, page_size: int = MAX_PAGE_SIZE
    ) -> dict[str, Any]:
        """类目商品列表（按销量排序），返回 result 字段。"""
        payload = {
            "currentPage": page,
            "pageSize": page_size,
            "catalogIdFilter": catalog_id,
            "brandIdFilter": "",
            "standardFilter": "",
            "brandPlaceFilter": "",
            "brandOriginFilter": "",
            "labelFilter": "",
            "arrangeFilter": "",
            "smtLabelFilter": "",
            "spotFilter": 1,
            "discountFilter": 1,
            "startPrice": "",
            "endPrice": "",
            "sortNumber": SORT_BY_SALES,
            "queryParameterValue": "",
            "lastParamName": "",
            "keyword": "",
            "secondKeyword": "",
            "hasDataFile": False,
            "demandNumber": "",
            "satisfyStockType": "",
            "queryProductTypeCode": "",
        }
        resp = await self._request(
            "POST",
            "https://list.szlcsc.com/category/product",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        if data.get("code") != 200:
            raise LcscError(f"类目商品列表异常 catalog={catalog_id} page={page}: {data.get('msg')}")
        return data["result"]

    async def brand_products_page(
        self, brand_id: int, page: int, page_size: int = MAX_PAGE_SIZE
    ) -> dict[str, Any]:
        """品牌商品列表（按销量排序），返回 result 字段。"""
        params = {
            "currentPage": page,
            "pageSize": page_size,
            "catalogIdFilter": "",
            "brandIdFilter": brand_id,
            "standardFilter": "",
            "brandPlaceFilter": "",
            "labelFilter": "",
            "arrangeFilter": "",
            "smtLabelFilter": "",
            "spotFilter": 1,
            "discountFilter": 1,
            "startPrice": "",
            "endPrice": "",
            "sortNumber": SORT_BY_SALES,
            "queryParameterValue": "",
            "lastParamName": "",
            "keyword": "",
            "secondKeyword": "",
            "hasDataFile": "false",
            "demandNumber": "",
            "satisfyStockType": "",
        }
        resp = await self._request(
            "GET", "https://list.szlcsc.com/brand/product", params=params
        )
        data = resp.json()
        if data.get("code") != 200:
            raise LcscError(f"品牌商品列表异常 brand={brand_id} page={page}: {data.get('msg')}")
        return data["result"]

    # ---------- 品牌 ----------

    async def brand_map(
        self, top_catalog_ids: Optional[list[int]] = None
    ) -> dict[str, list[dict]]:
        """按一级类目筛选的品牌目录: {字母: [{brandId, brandName, firstLetter}]}"""
        ids = top_catalog_ids or DEFAULT_TOP_CATALOG_IDS
        resp = await self._request(
            "GET",
            "https://list.szlcsc.com/brand/page/catalog",
            params={"catalogIds": ",".join(f"{i}," for i in ids)},
            headers={"Referer": "https://www.szlcsc.com/brand.html"},
        )
        data = resp.json()
        if data.get("code") != 200:
            raise LcscError(f"品牌目录接口异常: {data.get('msg')}")
        return data["result"].get("brandMap") or {}

    # ---------- 商品详情 ----------

    async def product_detail(self, product_id: str | int) -> dict[str, Any]:
        """商品详情页 webData（含完整参数表 paramList / brandVO / productRecord）。"""
        resp = await self._request("GET", f"https://item.szlcsc.com/{product_id}.html")
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            raise LcscError(f"详情页未找到 __NEXT_DATA__: productId={product_id}")
        data = json.loads(m.group(1))
        return data["props"]["pageProps"].get("webData") or {}

    async def substitute_models(self, product_code: str, product_id: str | int = "") -> list[str]:
        """替代/关联产品型号：POST /substitute/product/list?productCode=xxx。

        返回型号字符串列表（对应详情页「关联(替代产品)型号」）。
        """
        if not product_code:
            return []
        referer = (
            f"https://item.szlcsc.com/{product_id}.html"
            if product_id
            else "https://item.szlcsc.com/"
        )
        resp = await self._request(
            "POST",
            "https://list.szlcsc.com/substitute/product/list",
            params={"productCode": product_code},
            headers={"Content-Type": "application/json", "Referer": referer},
        )
        data = resp.json()
        all_items = (data.get("result") or {}).get("all") or []
        models: list[str] = []
        for x in all_items:
            vo = x.get("productVO") or x
            model = vo.get("productModel")
            if model:
                models.append(str(model))
        return models

    # ---------- 品牌详情（厂商官网） ----------

    async def brand_detail(self, brand_id: str | int) -> dict[str, Any]:
        """品牌详情页的 currentBrand（含 companyWebsite 厂商官网、logoUrl 等）。

        品牌页 `list.szlcsc.com/brand/{id}.html` 带 `_xvasu` 型 WAF，
        `_request` 会用 Node 执行挑战脚本取 cookie 后重试。
        """
        resp = await self._request("GET", f"https://list.szlcsc.com/brand/{brand_id}.html")
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            return {}
        data = json.loads(m.group(1))
        page_props = (data.get("props") or {}).get("pageProps") or {}
        search = (page_props.get("brandResult") or {}).get("searchResult") or {}
        return search.get("currentBrand") or {}
