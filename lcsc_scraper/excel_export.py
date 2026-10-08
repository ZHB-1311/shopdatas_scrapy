"""Excel 导出：类目 / 品牌模式统一按「每个分组一个 sheet」输出，
商品参数全部合并进单列 `参数`（形如 `名称:值；名称:值`）。"""

from __future__ import annotations

import re
import time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .scraper import DATA_DIR, ScrapeTask

BASE_COLUMNS = [
    "商品编号", "型号", "品牌", "品牌网址", "品牌简介", "类目", "商品描述", "封装",
    "库存", "近期销量", "最小起订", "包装方式", "包装规格", "单价", "价格梯度",
    "毛重", "图片链接", "数据手册PDF链接", "关联(替代产品)型号", "详情链接", "简介/备注",
    "参数",
]

PARAM_PREFIX = "参数:"
PARAM_COLUMN = "参数"
PARAM_SEP = "；"          # 参数之间用全角分号分隔
PARAM_KV_SEP = ":"        # 名称与值之间用半角冒号

HEADER_FILL = PatternFill("solid", fgColor="1F7AE0")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)


def format_params(product: dict) -> str:
    """把商品的 `参数:xxx` 字段合并成单列文本：`名称:值；名称:值；…`。

    保持参数在商品 dict 中的出现顺序（列表页 paramLinkedMap → 详情页 paramList 覆盖/补充），
    跳过空值、`-`（详情页用 `-` 表示无此项）与无名参数。

    值本身若含分号（立创的多值参数，如 `额定电流=3A；1A`，实测 190 处 / 73 个商品），
    会把 `；` 换成 `,`——否则下游按 `；` 拆分会串位。值里的冒号（如 `收缩率=2:1`）保留不动。
    """
    parts: list[str] = []
    for key, value in product.items():
        if not isinstance(key, str) or not key.startswith(PARAM_PREFIX):
            continue
        name = key[len(PARAM_PREFIX):].strip()
        text = "" if value is None else str(value).strip()
        text = text.replace("；", ",").replace(";", ",")
        if not name or text in ("", "-"):
            continue
        parts.append(f"{name}{PARAM_KV_SEP}{text}")
    return PARAM_SEP.join(parts)


def _safe_sheet_name(name: str, used: set[str]) -> str:
    name = re.sub(r"[\\/*?:\[\]]", " ", name).strip() or "sheet"
    name = name[:28]
    candidate = name
    i = 2
    while candidate in used:
        candidate = f"{name}~{i}"
        i += 1
    used.add(candidate)
    return candidate


def _style_header(ws) -> None:
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"


def _auto_width(ws, max_width: int = 46) -> None:
    for col_idx in range(1, ws.max_column + 1):
        width = 10
        for row_idx in range(1, min(ws.max_row, 60) + 1):
            v = ws.cell(row=row_idx, column=col_idx).value
            if v is not None:
                s = str(v)
                # 中文字符按2个宽度计
                w = sum(2 if ord(c) > 127 else 1 for c in s[:80])
                width = max(width, min(w + 2, max_width))
        # 参数列内容长，给足宽度（上限放宽，便于一眼看完）
        if ws.cell(row=1, column=col_idx).value == PARAM_COLUMN:
            width = 80
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _write_category_sheet(ws, products: list[dict]) -> None:
    columns = BASE_COLUMNS
    ws.append(columns)
    _style_header(ws)
    for p in products:
        ws.append([format_params(p) if c == PARAM_COLUMN else p.get(c) for c in columns])
    _auto_width(ws)


def export_task(task: ScrapeTask) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    site_label = {"lcsc": "立创", "hqchip": "华秋", "ickey": "云汉"}.get(task.site, task.site)
    name = (
        f"{site_label}商品数据_{task.mode}_"
        f"{'_'.join(list(task.groups)[:3])[:30]}_{stamp}.xlsx"
    )
    # 文件名去掉 Windows 非法字符
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    path = DATA_DIR / name

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for group, products in task.products.items():
        ws = wb.create_sheet(_safe_sheet_name(group, used))
        _write_category_sheet(ws, products)
    if not wb.sheetnames:
        ws = wb.create_sheet("无数据")
        ws.append(["未采集到数据"])
    wb.save(path)
    return path
