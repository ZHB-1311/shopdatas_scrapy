# AGENTS.md

## 项目说明

`lcsc_scraper/` 是电子元器件商城（立创 / 华秋 / 云汉）商品数据采集系统，采集结果导入服务器数据库 `nshop_tp8_v2` 使用。

## 关键约束（必须遵守）

### 数据库表 `sp_goods` 的 `Product_Source = 1` 是核心标识

- `sp_goods.Product_Source = 1` 的商品是**整个项目最重要、不可触碰的数据**：采集只读、绝不写入或修改
  （`lcsc_scraper/db.py` 只有 `SHOW COLUMNS` 与 `SELECT`，没有任何 INSERT/UPDATE/DELETE）。
- **`Product_Source = 1` 的商品参与去重判断**（2026-10 经用户明确同意后从「排除」改为「纳入」）：
  去重读取使用全部行（`excludeProductSource: null`，`WHERE` 子句不再过滤 `Product_Source`）。
  - 改这一点的原因（实测）：PS=1 共 4700 条，其中 **4694 条没有 `rsku_hidden`**（编号规则天然匹配不到），
    只填了 `sku`（型号）。若把 PS=1 整体排除，这些商品——包括店铺里已上架的——会被重复采回；
    实测用户截图 11 行里有 7 行属此类（如 `id=4092`，sku = `HYC23-HDMID19-650`，
    对应店铺页面 `dzyjzj.com/index/goods/detail/?id=4092`）。
  - 纳入后索引规模：274147 → **274153** 个编号键、274286 → **278981** 个型号键（型号规则是拦住它们的唯一有效键）。
  - `excludeProductSource` 仍保留为实现机制：填具体值时排除该 `Product_Source`，`null`（默认）= 不排除。
- **任何涉及 `Product_Source`（尤其是 `= 1` 的取值、判断、过滤、写入）的改动，必须先征得用户明确同意后才能进行。**
- 未经用户同意，不得修改、删除、绕过或弱化任何与 `Product_Source = 1` 相关的逻辑。

### 采集去重字段

- 采集去重有**两条规则，命中任一即跳过**：
  1. 采集到的**「商品编号」**对比 `sp_goods.rsku_hidden`；
  2. 采集到的**「型号」**对比 `sp_goods.sku`。
- **`rsku_hidden` 存储格式**：实际存的是「竖线包裹」的单值，形如 `|C367017|`（实测 27 万行均为单值，
  无多值行）。代码读取时按 `|` 拆分、去空白后取裸值，才能与采集到的商品编号比对；**直接等值比较会永远不命中**。
- **`sku` 存储内容**：存的是型号 / 规格（如 `0-1217891-1`、`(G)BM07B-SRSS-TB(LF)(SN)(P)`），
  **不是**商品编号，需与采集到的「型号」字段比对（`商品编号` vs `sku` 几乎不会命中）。
  比对前按 `db.normalize_model` 归一化：NFKC 全角转半角 → 去掉所有空白 → 转小写，
  以消除站点与库中的书写差异。
- 读取 SQL：`SELECT rsku_hidden, sku FROM sp_goods`（默认不再按 `Product_Source` 过滤，见上节；
  若配置了 `excludeProductSource` 为具体值，才追加 `WHERE (Product_Source IS NULL OR Product_Source <> 该值)`）。
- 商品编号字段由 `codeColumn` 指定（默认 `rsku_hidden`）；留空则按候选名自动探测
  （见 `lcsc_scraper/db.py` 的 `CANDIDATE_CODE_COLUMNS`，`rsku_hidden` 优先）。
- 型号字段由 `skuColumn` 指定（默认 `sku`）；留空 `""` 表示**关闭按型号去重**；字段不存在时只关闭该规则，
  不影响商品编号去重。
- 排除值由 `excludeProductSource` 指定：`null`（默认，**不排除**，即 PS=1 也参与去重）；填具体值则排除该
  `Product_Source`。改动此项须先经用户同意。
- 配置来源优先级（后者覆盖前者）：内置默认 → `lcsc_scraper/db_config.json` →
  环境变量 `DB_CODE_COLUMN` / `DB_SKU_COLUMN` → 网页「商品编号字段 / 型号字段」。
- 读取入口为 `db.fetch_existing_index()`（返回 `ExistingIndex`，含 `codes` / `skus` / `error`，
  判断统一走 `ExistingIndex.matched(商品编号, 型号)`）。
- `sp_goods` 共 92 个字段（`id` / `seller_id` / `sku` / `osku` / `name` / `rsku_hidden` /
  `Product_Source` …），表前缀 `sp_`。核对字段名与连接可用：`py -3.12 -m lcsc_scraper.db`。
- 如需修改对比字段、归一化方式或排除条件，须先经用户同意。

### 图片过滤（imageFilter）

- 部分商品的「图片链接」不是商品实拍图，而是**站点给的资质图 / 占位图**，导入商城等同于无图，故默认跳过：
  - 立创：URL 含 `/upload/public/brand/product/certificate/` —— **品牌证书图**（同一品牌下所有商品共用一张，
    实测 860/8543 条仅 16 张不同图，商品卡片缩略图因此完全相同）；
  - 云汉：URL 含 `default_list_logo` —— 默认占位图（89 条共用 1 张）。
- URL 特征集中在 `lcsc_scraper/scraper.py` 的 `NON_PRODUCT_IMAGE_MARKS`，
  判定函数为 `is_non_product_image()` / `has_product_image()` / `passes_image_filter()`。
- 三档模式（任务配置 `imageFilter`，网页「图片过滤」下拉，默认 `placeholder`）：
  - `placeholder`（默认）：跳过非商品图，**无图商品照常采集**（实测跳过约 11.6%）；
  - `require`：连同**无图**商品一起跳过（无图实测约 44%，会让目标条数大幅下调，慎用）；
  - `off`：不过滤（旧行为）。
- 判断时机：在 `enrich()` **之后**。华秋等站点的列表行不带图片（`图片链接: ""`），图片只在详情页才有，
  若在列表页判断会把「暂未取到图」误判为「无图」而整站过滤。
- 被跳过的条数计入 `task.skipped_no_image`（日志「跳过图片不合规 N 条」、前端「跳过(图片)」），
  并从该组目标数中扣除（与数据库去重同一套口径）。
- 预览区「只看有图」用的也是 `has_product_image()`：证书图 / 占位图不算有图。

### 采集过滤（图片 / 数据手册）

- 采集时按三组规则过滤，命中即跳过、不计入目标（与数据库去重同一套口径：计入 `task.skipped_*`、
  日志打印、从该组目标数中扣除）：
  1. **数据库去重**（见上节）；
  2. **图片过滤** `imageFilter`（见下节，默认 `placeholder`）；
  3. **数据手册过滤** `datasheetFilter`（默认开）：跳过没有数据手册 PDF 链接的商品。
- **数据手册过滤只对「站点提供该字段」的站点生效**（`supports_datasheet` 类属性：立创 `True`，
  华秋 / 云汉 `False`）。原因：华秋 / 云汉 全站 0% 有数据手册（其解析模块连 PDF 字段都没有），
  无条件启用会把这两站的商品全部跳过。实测立创有值率 92.9%。
- 判断时机都在 `enrich()` **之后**（与图片过滤一致，避免列表页字段未就绪时误杀），跳过条数分别计入
  `task.skipped_no_image` / `task.skipped_no_datasheet`，前端显示「跳过(图片)」「跳过(无手册)」。

### Excel 导出结构（2026-10 起）

- 共 **24 列**：`商品编号 / 型号 / 品牌 / 公司名称 / 公司logo / 品牌网址 / 品牌简介 / 类目 / 商品描述 / 封装 /
  库存 / 近期销量 / 最小起订 / 包装方式 / 包装规格 / 单价 / 价格梯度 / 毛重 / 图片链接 / 数据手册PDF链接 /
  关联(替代产品)型号 / 详情链接 / 简介/备注 / 参数`。
- **文本清洗**（2026-10）：站点给的简介/描述常带 HTML 实体（如 `&nbsp;&nbsp;C&K成立于…`），
  统一走 `company.clean_text()`（反转义实体 ≤3 层、去标签、归一空白、`&nbsp;`→空格）。
  来源处已清洗：立创 `品牌简介/品牌网址/商品描述/简介备注`、华秋 `品牌简介/商品描述/简介备注`、
  云汉 `简介/备注`；`excel_export._cell_value` 再对所有文本列兜底清洗一次。
- **`公司名称`（2026-10 新增）位于 `品牌` 之后**：由 `品牌简介` 经 `company.extract_company_name()`
  分三层提取——① 前缀法（取起始到第一个触发词/标点的片段，如 `泰科电子（TE Connectivity）`）；
  ② 实体法（全文找第一个以公司后缀结尾的实体，后缀按特异性排序，处理简介不以公司名开头的情况，
  如 `深圳市长江连接器有限公司`）；③ 回退到品牌名（如 `TOPLY(拓林)`）。
  导出时按列即时计算（`excel_export._cell_value`，传入 `品牌` 作回退）。
- **`公司logo`（2026-10 新增）紧跟 `公司名称` 之后**：品牌 logo 图片 URL（每品牌一张，与品牌元信息同源）。
  - 立创：`client.brand_meta()` 返回的 `currentBrand.logoUrl`（实测 `alimg.szlcsc.com/upload/public/brand/logo/…`
    图床；少数品牌无 logo 则留空）。
  - 华秋：品牌页 `/gongsi/{公司ID}.html` 的 `div.ibox > img[src]`（协议相对地址 `//file.huaqiu.com/…`，
    解析时补 `https:`）；在 `HqChipClient.brand_meta()` 里与 `website/intro` 一并缓存。
  - 云汉：无品牌元信息，留空。
- **`引脚图链接` / `焊盘链接` 已彻底移除**：不再导出，也**不再抓取**（`LcscSite.enrich()` 里取消调用，
  每个商品少一次 `lceda.cn` 请求；`LcscClient.pinpad_urls()` 方法保留以便随时接回）。
- **参数合并为单列 `参数`**，格式 `名称:值；名称:值；…`（`excel_export.format_params()`）。
  参数值里自带的分号会被换成 `,`（立创多值参数如 `额定电流=3A；1A`，实测 190 处 / 73 个商品），
  否则下游按 `；` 拆分会串位；值里的冒号保留（如 `收缩率=2:1`）。空值与 `-` 不写入。
- 改列结构时同步改 `excel_export.BASE_COLUMNS`，并重跑既有文件的字段对齐。

### 数据库连接

- 目标库 `nshop_tp8_v2`（表前缀 `sp_`），连库**必须用 `utf8mb4`**，否则中文乱码（代码已固定 `charset="utf8mb4"`）。
- 实际连接参数见 `lcsc_scraper/db_config.json`（含密码，`.gitignore` 已忽略 `/lcsc_scraper`，不入库）。
- 连接参数：`127.0.0.1:3307`（本机 docker 容器 `mysql8-new` 映射的端口；容器内为 3306），用户 `root`。
- 连不上时采集不会被阻断：只在日志提示原因（如 `Access denied` / `Unknown database`），本次不做已有商品过滤。
- 不要把密码写进本文件或任何会被提交的文件。

## 反爬与品牌模式注意事项

### 华秋（hqchip）类目与登录态（2026-10）

- **类目菜单源改为 `https://www.hqchip.com/app`**：原 `/app/cn` 已变成登录壳页（36KB，无
  `second-category-title`、无 `/app/` 链接），导致 `catalog_groups()` 返回 `[]`、前端类目勾选区全空。
  首页/`/app` 仍内嵌完整菜单。**分组与叶子链接由绝对路径 `https://www.hqchip.com/app/{id}` 改成了
  相对路径 `href="/app/{id}"`**，`hqchip.py` 两条正则已改为可选前缀
  `(?:https://www\.hqchip\.com)?/app/(\d+)`。三大目标分组（连接器 1512 / 开关及按键 848 /
  线缆及配套 1356）ID 未变。
- **列表接口现要求登录态**：`GET /category/detailInfo.html` 无 Cookie 时返回
  `retCode 105000 / retMsg「请先登录」`（自 2026-10 起，此前可匿名访问）。站点自发的访客 Cookie
  （`acw_tc` / `cdn_sec_tc` / `visitor`）不够，搜索页也会 302 到 `users/login.html`。
- **解决方式**：给 `HqChipClient` 注入浏览器登录 Cookie。优先级：任务配置 `cookie` >
  `lcsc_scraper/hqchip_config.json` 的 `cookie` 字段 > 环境变量 `HQCHIP_COOKIE`。
  前端「华秋登录 Cookie」文本域填入（`index.html`），经 `TaskCreate.cookie` →
  `create_site(..., config)` → `HqSite(cookie=...)` → `HqChipClient(cookie=...)` 注入 `Cookie` 头。
- 取 Cookie：已登录的浏览器 F12 → Network → 任一 `hqchip.com` 请求 → Request Headers 里的 `Cookie` 整串。
  Cookie 等同登录凭证，`/lcsc_scraper` 已被 `.gitignore` 忽略，不要提交。
- 未配置 Cookie 时客户端会 warning；列表接口命中 105000 时抛出带指引的 `LcscError`。

### 华秋（hqchip）字段映射（2026-10）

- 华秋数据分两处取：**列表接口**（`/category/detailInfo.html`，需登录）给骨架字段；
  **详情页**（`item.hqchip.com/{stockId}.html`，公开）给完整字段；**品牌页**给品牌网址/简介。
- 详情页解析（`HqChipClient.product_detail`）：
  - 价格梯度：`<h2 class="price-list">` 内第一个 `<table>` 的「数量 / 国内含税」行 →
    `1+: ¥2.21650; 30+: ¥2.13900; …`（与立创同格式）。注意排除「其他供应商价格」表。
  - **单价 = 价格梯度里「数量档最大」那档的价格**（如 `1000+` 档，即最低价，`price_bulk`）——
    与立创口径一致（`LcscSite._flatten` 的 `max_tier`）。华秋列表接口的 `shop_price` 常为 `0`
    （实测），故以详情页阶梯价覆盖。
  - 包装：`<span class="s3">「袋装(BAG)/10」</span>` → 包装方式=袋装、包装规格=10。
  - 商品描述：`<span class="text goodsDesc">…`。
  - 品牌公司ID：`<a class="brandName" href="/gongsi/{id}.html">`。
  - 图片：`goodsImg` 数组；部分商品确实无图（页面用 `nob.png` 占位），此时图片为空。
- 品牌网址 / 品牌简介（`HqChipClient.brand_meta`）：`GET /gongsi/{公司ID}.html` 的
  `.brand_url a[href]`（官网）与 `.introduce_text`（简介），按公司ID 缓存 + 并发锁。
- 类目：列表接口只给叶子名，`HqSite` 用 `_leaf_top`（叶子ID→顶级分组）拼成「大类>子类」，
  如 `开关及按键>滑动开关`。
- **字段重映射（按用户要求）**：`商品描述` 列 = 型号；站点原「商品描述」（详情页 goodsDesc）
  放入 `简介/备注` 列；`包装方式/包装规格/价格梯度/品牌网址/品牌简介` 由详情页/品牌页补全。

### 采集性能（2026-10）

- **详情请求并发**：`_scrape_group` 按 `min(单请求并发数, 10)` 分批 `asyncio.gather` 抓详情，
  再按列表顺序逐条消费（不改变「销量前 N 条」语义）。实测详情单条 ~1.9s，10 并发 ~2.0s。
- **列表阶段预过滤**：立创列表行已带最终「图片链接」「数据手册PDF链接」（`enrich` 不改写这两项），
  故 `LcscSite.list_image_ready / list_datasheet_ready = True`，在发详情请求前先过滤，
  省掉被拒商品的那次详情请求（华秋/云汉为 False，因图只在详情页）。实测 18 组任务 285s → 61s。

### 立创品牌页反爬

- 立创品牌页 `list.szlcsc.com/brand/{id}.html` 目前被**腾讯验证码（TCaptcha）拦截**，返回的不是
  `__NEXT_DATA__` 而是验证码脚本，因此 `brand_page_info` 会得到空结果。
- 后果：若把空结果当成「品牌无商品」，品牌模式会静默漏采（曾出现「命中 0 个子类目／无在售商品」的误报）。
  因此 `brand_info` 返回 `None`（=探测失败），`brand_scope` 据此**抛错**，`_run_brand` 再降级。
- 降级路径：`LcscSite.brand_leaf_counts()` 用可用的「类目 + 品牌」列表接口
  （`POST list.szlcsc.com/category/product`，带 `brandIdFilter`）逐子类目取 `totalCount`，
  得到精确的「品牌 × 子类目」条数（与站点页面「共 N 件相关商品」一致）。
- 排查要点：品牌模式日志若出现「品牌页探测失败，改用「类目+品牌」逐类目统计」即为该降级生效，属正常。
- **品牌网址 / 品牌简介 已改用可用接口**（2026-10 修复）：原先取品牌页 `currentBrand`，因品牌页被拦截
  导致这两列**全空**（实测 8100 行 / 71 个品牌 0 行有值；品牌页对任意 brandId 都只返回 1697 字节的
  验证码壳，`list` 与 `www` 域名都一样；详情接口的 `brandVO` 也只有 brandName/brandId）。
  现改为 `GET https://list.szlcsc.com/brand/product?brandIdFilter={品牌ID}&pageSize=1` 的
  `result.searchResult.currentBrand`（含 `companyWebsite` 厂商官网、`companyContext` 品牌简介），
  该接口未被拦截，是品牌模式本来就在用的接口族。实现：`LcscClient.brand_meta()` +
  `LcscSite.brand_meta()`（按品牌ID 惰性缓存，每个品牌 1 次请求；探测失败缓存 None、字段留空）。
  `brand_info` / `brand_scope`（品牌页路线）保持不动，只服务品牌模式的子类目条数探测与降级逻辑。

## 常用命令

```bat
pip install -r lcsc_scraper\requirements.txt
lcsc_scraper\start.bat            :: 启动网页界面 http://127.0.0.1:8123
```
