#!/usr/bin/env python3
"""
Google Sheet 输出目标。

支持两种鉴权方式（实例配置的 auth）：
- service_account（缺省）：服务端无人值守定时运行，OAuth 用户授权需要回调地址、要存 refresh token，
  还会在 token 失效后要求有人重新点一次授权。使用方把表格共享给服务账号的邮箱（编辑者权限）。
- gogcli：复用构建机上 gogcli 已登录的用户身份，服务不持有任何用户令牌（ADR-0005）。

请求怎么发出去由 google_transport.py 负责，这里只描述调哪个接口、带什么参数，
因此推送、检查、表头定位与补回规则两种方式共用一份。
"""

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .base import (
    CHECK_ERROR,
    CHECK_OK,
    CHECK_WARN,
    COLUMN_DATETIME,
    COLUMN_NUMBER,
    CheckItem,
    Column,
    Destination,
    DestinationError,
    LedgerRow,
    PushStats,
    TargetField,
)
from .google_transport import AuthRejected, GogcliTransport, ServiceAccountTransport, SheetsTransport

logger = logging.getLogger(__name__)

AUTH_SERVICE_ACCOUNT = "service_account"
AUTH_GOGCLI = "gogcli"

# 单元格字符上限是 Google 的硬限制，超出会让整批写入失败，而不是只截断那一格
CELL_CHAR_LIMIT = 50000
TRUNCATED_SUFFIX = "\n…（内容过长已截断，完整内容见运行详情）"

# 单次请求的负载上限。Google 建议不超过 2MB；台账正文可能很长（每行上限五万字），
# 不分批的话几百行就能撞上请求体上限。
MAX_BATCH_BYTES = 2_000_000

_SPREADSHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_SPREADSHEET_URL_PATTERN = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")

# 工作表名上限是 Google 的限制
MAX_WORKSHEET_TITLE = 100

# 进程内的"每张工作表一把锁"。
# 两个 Survey 共用一张工作表时键不同（slug:指纹），不会写串；但"读键列 → 追加"不是原子的，
# 两次推送交错时，后一次读到的行号可能已经被前一次的追加挪动过。跨进程不加锁：
# 同一 Binding 的并发由 survey_push 的进行中记录挡住，剩下的只有跨 Survey 共用一张表的情况，
# 为它引入分布式锁不成比例。
_sheet_locks: Dict[Tuple[str, str], threading.Lock] = {}
_sheet_locks_guard = threading.Lock()


def _sheet_lock(spreadsheet_id: str, worksheet: str) -> threading.Lock:
    """取某张工作表的进程内锁。"""
    with _sheet_locks_guard:
        return _sheet_locks.setdefault((spreadsheet_id, worksheet), threading.Lock())


def column_letter(index: int) -> str:
    """0 起始的列序号转 A1 记法的列字母：0 → A，25 → Z，26 → AA。"""
    letters = ""
    number = index + 1
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def quote_sheet_title(title: str) -> str:
    """A1 记法里的工作表名：一律加单引号，内部单引号写两遍。不加引号的话含空格或中文括号的名字会解析失败。"""
    return "'" + title.replace("'", "''") + "'"


def parse_spreadsheet_id(value: str) -> str:
    """从表格 ID 或表格链接中取出 ID；取不出时抛 ValueError。"""
    raw = str(value or "").strip()
    match = _SPREADSHEET_URL_PATTERN.search(raw)
    if match:
        raw = match.group(1)
    if not _SPREADSHEET_ID_PATTERN.match(raw):
        raise ValueError("无法识别的表格 ID，请填写表格链接或链接中 /d/ 与 /edit 之间的那一段")
    return raw


def _cell_value(column: Column, value: Any) -> Any:
    """
    把逻辑值转成写入单元格的值。

    空值写成空串而不是 null：Sheets 接口会**跳过** null，
    那样上一轮写进去的旧值（比如已经不存在的行号）会一直留在格子里。
    """
    if value is None:
        return ""
    if column.kind == COLUMN_NUMBER:
        return value
    if column.kind == COLUMN_DATETIME and isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if len(text) > CELL_CHAR_LIMIT:
        text = text[: CELL_CHAR_LIMIT - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX
    return text


def _chunks_by_size(items: List[Any], max_bytes: int = MAX_BATCH_BYTES) -> List[List[Any]]:
    """按序列化后的大小把列表切成若干批。单个元素超限时独占一批。"""
    batches: List[List[Any]] = []
    current: List[Any] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if current and size + item_size > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += item_size
    if current:
        batches.append(current)
    return batches


class GoogleSheetDestination(Destination):
    """把 Ledger 镜像到一张 Google Sheet 的某个工作表。"""

    type_name = "google_sheet"
    label = "Google Sheet"
    target_fields = (
        TargetField(
            key="spreadsheet",
            label="表格",
            placeholder="表格链接或表格 ID",
            help="需要先把表格以「编辑者」身份共享给写表的身份：服务账号邮箱，或 gogcli 中授权的账号。",
        ),
        TargetField(
            key="worksheet",
            label="工作表",
            required=False,
            placeholder="留空则使用巡检名称",
            help="不存在时自动创建。多个巡检可以共用一张工作表，靠「巡检」列区分。",
        ),
    )

    def __init__(
        self,
        name: str,
        options: Dict[str, Any],
        session: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
        runner: Optional[Callable[..., Any]] = None,
    ):
        """session / runner / sleep 仅供测试注入：分别替换服务账号的 HTTP 会话与 gogcli 的子进程调用。"""
        super().__init__(name, options)
        extra = {"sleep": sleep} if sleep else {}
        if self.auth == AUTH_GOGCLI:
            self._transport: SheetsTransport = GogcliTransport(
                account=str(options.get("account") or "").strip(),
                binary=str(options.get("gogcli_bin") or "gog").strip(),
                **({"runner": runner} if runner else {}), **extra,
            )
        else:
            self._transport = ServiceAccountTransport(
                str(options.get("credentials_file") or ""), session=session, **extra,
            )
        # 追加列要用数值 sheetId 而不是标题，由 _ensure_worksheet 填入
        self._sheet_id: Optional[int] = None

    @property
    def auth(self) -> str:
        """鉴权方式。缺省为服务账号，0.7.0 之前写好的实例配置不受影响。"""
        return str(self.options.get("auth") or AUTH_SERVICE_ACCOUNT).strip()

    @classmethod
    def validate_options(cls, options: Dict[str, Any]) -> None:
        """
        按鉴权方式校验必填项。文件或可执行文件是否存在留到推送时检查，以免挂载延迟时整个实例被判为不可用。
        """
        options = options or {}
        auth = str(options.get("auth") or AUTH_SERVICE_ACCOUNT).strip()
        if auth == AUTH_SERVICE_ACCOUNT:
            if not str(options.get("credentials_file") or "").strip():
                raise DestinationError("缺少 credentials_file（服务账号 JSON 密钥文件路径）")
        elif auth == AUTH_GOGCLI:
            account = str(options.get("account") or "").strip()
            # 必须显式指定：别人在构建机上执行一次 `gog auth add` 就能换掉默认账号，
            # 推送随之以另一个人的身份写表
            if not account or account.lower() in {"auto", "default"}:
                raise DestinationError("auth 为 gogcli 时必须填写 account（gogcli 中已授权的账号邮箱）")
        else:
            raise DestinationError(f"未知的鉴权方式：{auth}；可选 {AUTH_SERVICE_ACCOUNT} 或 {AUTH_GOGCLI}")

    @classmethod
    def describe_options(cls, options: Dict[str, Any]) -> str:
        """鉴权方式；gogcli 方式带上账号，方便在下拉框里分清同一张表的不同身份。"""
        options = options or {}
        if str(options.get("auth") or AUTH_SERVICE_ACCOUNT).strip() == AUTH_GOGCLI:
            return f"gogcli · {str(options.get('account') or '').strip()}"
        return "服务账号"

    @classmethod
    def normalize_target(cls, target: Dict[str, Any], survey_name: str) -> Dict[str, str]:
        """表格允许填链接，落库只存 ID；工作表留空时取巡检名称。"""
        raw = target if isinstance(target, dict) else {}
        spreadsheet = str(raw.get("spreadsheet") or "").strip()
        if not spreadsheet:
            raise ValueError("表格不能为空")
        worksheet = str(raw.get("worksheet") or "").strip() or str(survey_name or "").strip()
        if not worksheet:
            raise ValueError("工作表不能为空")
        if len(worksheet) > MAX_WORKSHEET_TITLE:
            raise ValueError(f"工作表名称不能超过 {MAX_WORKSHEET_TITLE} 个字符")
        return {"spreadsheet": parse_spreadsheet_id(spreadsheet), "worksheet": worksheet}

    # ------------------------------------------------------------------
    # 连通性测试
    # ------------------------------------------------------------------

    def check(self, target: Dict[str, str], columns: Sequence[Column]) -> List[CheckItem]:
        """依次检查：身份 → 读取表格 → 工作表与表头 → 写入权限。不创建工作表，不写任何台账行。"""
        items = self._transport.preflight()
        if not items or items[-1].level == CHECK_ERROR:
            return items

        spreadsheet = target["spreadsheet"]
        try:
            meta = self._transport.call(
                "spreadsheets.get",
                {"spreadsheetId": spreadsheet, "fields": "properties.title,sheets.properties(sheetId,title)"},
                quick=True,
            )
        except AuthRejected as e:
            # 凭据能加载不代表有效，第一次真正请求时才会去换令牌；结论归到身份那一项
            return self._replace_auth_item(items, str(e))
        except DestinationError as e:
            items.append(CheckItem("读取表格", CHECK_ERROR, str(e)))
            return items
        title = (meta.get("properties") or {}).get("title") or ""
        items.append(CheckItem("读取表格", CHECK_OK, f"表格「{title}」可以访问"))

        worksheet = target["worksheet"]
        titles = [(s.get("properties") or {}).get("title") for s in meta.get("sheets") or []]
        if worksheet not in titles:
            items.append(CheckItem("工作表", CHECK_WARN, f"工作表「{worksheet}」不存在，首次推送时会自动创建"))
        else:
            try:
                data = self._transport.call(
                    "spreadsheets.values.get",
                    {"spreadsheetId": spreadsheet, "range": quote_sheet_title(worksheet) + "!1:1"},
                    quick=True,
                )
            except DestinationError as e:
                items.append(CheckItem("工作表", CHECK_ERROR, str(e)))
                return items
            headers = {str(v).strip() for v in ((data.get("values") or [[]])[0] or [])}
            present = [c.header for c in columns if c.header in headers]
            if len(present) == len(columns):
                message = f"工作表「{worksheet}」已存在，系统列齐全"
            elif present:
                missing = "、".join(c.header for c in columns if c.header not in headers)
                message = (
                    f"工作表「{worksheet}」已存在，已有 {len(present)}/{len(columns)} 个系统列；"
                    f"缺少的（{missing}）会在首次推送时补在现有表头之后"
                )
            elif headers:
                message = f"工作表「{worksheet}」已存在但没有任何系统列，推送时会把系统列补在现有表头之后"
            else:
                message = f"工作表「{worksheet}」已存在且是空表，首次推送时写入表头"
            items.append(CheckItem("工作表", CHECK_OK, message))

        try:
            # 用"把表格标题改成它自己"验证写权限：这是一次真实的写请求，查看者会被拒绝，
            # 但表格内容与标题都不变。只读 GET 证明不了能写，而写一行再删掉会在用户的表里留下痕迹。
            self._transport.call(
                "spreadsheets.batchUpdate",
                {"spreadsheetId": spreadsheet},
                body={"requests": [{"updateSpreadsheetProperties": {
                    "properties": {"title": title}, "fields": "title",
                }}]},
                quick=True,
            )
        except DestinationError as e:
            items.append(CheckItem("写入权限", CHECK_ERROR, str(e)))
            return items
        items.append(CheckItem("写入权限", CHECK_OK, "可以写入"))
        return items

    def _replace_auth_item(self, items: List[CheckItem], message: str) -> List[CheckItem]:
        """把身份那一项改判为失败，并丢弃它之后的检查项。"""
        for index, item in enumerate(items):
            if item.title == self._transport.auth_title:
                return items[:index] + [CheckItem(item.title, CHECK_ERROR, message)]
        return items + [CheckItem(self._transport.auth_title, CHECK_ERROR, message)]

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def push(self, target: Dict[str, str], columns: Sequence[Column], rows: List[LedgerRow]) -> PushStats:
        """把台账镜像到工作表。整体流程：定位工作表 → 补表头 → 读键列 → 更新已有行 → 追加缺失行。"""
        spreadsheet_id = target["spreadsheet"]
        worksheet = target["worksheet"]
        with _sheet_lock(spreadsheet_id, worksheet):
            return self._push_locked(spreadsheet_id, worksheet, list(columns), rows)

    def _push_locked(
        self, spreadsheet_id: str, worksheet: str, columns: List[Column], rows: List[LedgerRow]
    ) -> PushStats:
        """持锁执行的推送主体。"""
        sheet_ref = quote_sheet_title(worksheet)

        grid_columns = self._ensure_worksheet(spreadsheet_id, worksheet)
        positions, new_headers = self._resolve_columns(spreadsheet_id, sheet_ref, columns)

        needed = max(positions.values()) + 1
        if needed > grid_columns:
            # 写到网格之外会被接口直接拒绝，而不是自动扩列
            self._append_columns(spreadsheet_id, worksheet, needed - grid_columns)
        if new_headers:
            self._write_cells(spreadsheet_id, [
                {"range": f"{sheet_ref}!{column_letter(index)}1", "values": [[header]]}
                for index, header in new_headers
            ])

        key_column = columns[0]
        if key_column.header in {header for _, header in new_headers}:
            # 键列是刚补出来的，说明表里还没有任何台账行，不必再读
            existing: Dict[str, List[int]] = {}
        else:
            existing = self._read_keys(spreadsheet_id, sheet_ref, positions[key_column.key])

        stats = PushStats()
        updates: List[dict] = []
        appends: List[List[Any]] = []
        width = needed
        for row in rows:
            row_numbers = existing.get(row.key)
            if row_numbers:
                for number in row_numbers:
                    updates.extend(self._row_updates(sheet_ref, number, columns, positions, row))
                stats.updated += 1
            elif row.restore_if_missing:
                cells: List[Any] = [None] * width
                for column in columns:
                    cells[positions[column.key]] = _cell_value(column, row.values.get(column.key))
                appends.append(cells)
                stats.inserted += 1
            else:
                stats.skipped += 1

        if updates:
            self._write_cells(spreadsheet_id, updates)
        if appends:
            self._append_rows(spreadsheet_id, sheet_ref, appends)
        return stats

    def _ensure_worksheet(self, spreadsheet_id: str, worksheet: str) -> int:
        """确保工作表存在，返回它当前的列数。"""
        meta = self._transport.call(
            "spreadsheets.get", {"spreadsheetId": spreadsheet_id, "fields": "sheets.properties(sheetId,title,gridProperties)"}
        )
        for sheet in meta.get("sheets", []) or []:
            props = sheet.get("properties", {}) or {}
            if props.get("title") == worksheet:
                self._sheet_id = props.get("sheetId")
                return int((props.get("gridProperties") or {}).get("columnCount") or 26)

        reply = self._transport.call(
            "spreadsheets.batchUpdate", {"spreadsheetId": spreadsheet_id},
            body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
        )
        props = ((reply.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        self._sheet_id = props.get("sheetId")
        logger.info("Created worksheet %r in spreadsheet %s", worksheet, spreadsheet_id)
        return int((props.get("gridProperties") or {}).get("columnCount") or 26)

    def _append_columns(self, spreadsheet_id: str, worksheet: str, count: int) -> None:
        """在工作表末尾追加列。"""
        self._transport.call(
            "spreadsheets.batchUpdate", {"spreadsheetId": spreadsheet_id},
            body={"requests": [{"appendDimension": {
                "sheetId": self._sheet_id, "dimension": "COLUMNS", "length": int(count),
            }}]},
        )

    def _resolve_columns(
        self, spreadsheet_id: str, sheet_ref: str, columns: List[Column]
    ) -> Tuple[Dict[str, int], List[Tuple[int, str]]]:
        """
        读表头，按名称定位每个系统列。

        返回 ({column.key: 列序号}, [(列序号, 需要补写的表头)])。
        缺失的系统列补在现有表头之后，不插到中间 —— 插列会挪动用户的人工列。
        """
        data = self._transport.call("spreadsheets.values.get", {"spreadsheetId": spreadsheet_id, "range": sheet_ref + "!1:1"})
        header_row = [str(v).strip() for v in ((data.get("values") or [[]])[0] or [])]

        index_by_header: Dict[str, int] = {}
        for index, header in enumerate(header_row):
            if header and header not in index_by_header:
                index_by_header[header] = index

        positions: Dict[str, int] = {}
        new_headers: List[Tuple[int, str]] = []
        next_index = len(header_row)
        for column in columns:
            if column.header in index_by_header:
                positions[column.key] = index_by_header[column.header]
            else:
                positions[column.key] = next_index
                new_headers.append((next_index, column.header))
                next_index += 1
        return positions, new_headers

    def _read_keys(self, spreadsheet_id: str, sheet_ref: str, key_index: int) -> Dict[str, List[int]]:
        """读键列，返回 {键: [行号...]}。同一个键出现多行（用户复制过行）时每一行都更新。"""
        letter = column_letter(key_index)
        data = self._transport.call(
            "spreadsheets.values.get",
            {
                "spreadsheetId": spreadsheet_id, "range": f"{sheet_ref}!{letter}2:{letter}",
                "majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE",
            },
        )
        keys: Dict[str, List[int]] = {}
        for offset, cells in enumerate(data.get("values") or []):
            key = str(cells[0]).strip() if cells else ""
            if key:
                keys.setdefault(key, []).append(offset + 2)
        return keys

    @staticmethod
    def _row_updates(
        sheet_ref: str, row_number: int, columns: List[Column], positions: Dict[str, int], row: LedgerRow
    ) -> List[dict]:
        """
        一行已有记录的系统列更新，按连续列段拆成若干范围。

        不整行覆盖：系统列之间可能夹着用户插入的人工列，整行写会把它们清空。
        """
        cells = sorted((positions[c.key], _cell_value(c, row.values.get(c.key))) for c in columns)
        ranges: List[dict] = []
        start, values = cells[0][0], [cells[0][1]]
        for index, value in cells[1:]:
            if index == start + len(values):
                values.append(value)
                continue
            ranges.append(_range(sheet_ref, row_number, start, values))
            start, values = index, [value]
        ranges.append(_range(sheet_ref, row_number, start, values))
        return ranges

    def _write_cells(self, spreadsheet_id: str, data: List[dict]) -> None:
        """
        批量写入若干范围。

        valueInputOption 必须是 RAW：正文来自模型，以 "=" 开头的内容在 USER_ENTERED 下
        会被当成公式执行（例如 =IMPORTXML 把表内数据发往外部地址）。
        """
        for batch in _chunks_by_size(data):
            self._transport.call(
                "spreadsheets.values.batchUpdate", {"spreadsheetId": spreadsheet_id},
                body={"valueInputOption": "RAW", "data": batch},
            )

    def _append_rows(self, spreadsheet_id: str, sheet_ref: str, rows: List[List[Any]]) -> None:
        """
        在表格末尾追加行。

        人工列位置填 null：追加接口会跳过 null，不会往用户的列里写空串。
        INSERT_ROWS 让接口自动扩行，不受网格行数限制。
        """
        for batch in _chunks_by_size(rows):
            # 5xx 后重试追加存在重复写入的可能（请求其实已经生效）。影响有限：同一个键的多行
            # 在下次推送时都会被更新，数据不会出错，只是多出一行
            self._transport.call(
                "spreadsheets.values.append",
                {
                    "spreadsheetId": spreadsheet_id, "range": sheet_ref + "!A1",
                    "valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS",
                },
                body={"majorDimension": "ROWS", "values": batch},
            )


def _range(sheet_ref: str, row_number: int, start: int, values: List[Any]) -> dict:
    """一段连续单元格的写入范围。"""
    first = column_letter(start)
    last = column_letter(start + len(values) - 1)
    return {"range": f"{sheet_ref}!{first}{row_number}:{last}{row_number}", "values": [values]}
