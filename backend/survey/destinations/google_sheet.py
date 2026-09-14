#!/usr/bin/env python3
"""
Google Sheet 输出目标。

鉴权用 Service Account：服务端无人值守定时运行，OAuth 用户授权需要回调地址、
要存 refresh token，还会在 token 失效后要求有人重新点一次授权 —— 那一天巡检会静默地不再推送。
使用方需要把表格共享给服务账号的邮箱（编辑者权限）。

依赖只用 google-auth + 直接调 Sheets REST API，不引 gspread / google-api-python-client：
前者在批量更新时仍要自己拼范围，后者体积大、依赖多，而这里总共只用到五个接口。
"""

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .base import (
    COLUMN_DATETIME,
    COLUMN_NUMBER,
    Column,
    Destination,
    DestinationError,
    LedgerRow,
    PushStats,
    TargetField,
)

logger = logging.getLogger(__name__)

API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# 单元格字符上限是 Google 的硬限制，超出会让整批写入失败，而不是只截断那一格
CELL_CHAR_LIMIT = 50000
TRUNCATED_SUFFIX = "\n…（内容过长已截断，完整内容见运行详情）"

# 单次请求的负载上限。Google 建议不超过 2MB；台账正文可能很长（每行上限五万字），
# 不分批的话几百行就能撞上请求体上限。
MAX_BATCH_BYTES = 2_000_000

# 写入配额是每分钟 60 次（按项目 + 用户），推送一个几百行的台账只需几次请求，
# 撞上 429 基本是多个 Survey 同时推送。退避上限 32 秒、最多 5 次，足够等过一个配额窗口。
MAX_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

REQUEST_TIMEOUT_SECONDS = 60

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
            help="需要先把表格以「编辑者」身份共享给服务账号的邮箱。",
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
        sleep: Callable[[float], None] = time.sleep,
    ):
        """session 与 sleep 仅供测试注入；生产环境按 credentials_file 惰性创建会话。"""
        super().__init__(name, options)
        self._session = session
        self._sleep = sleep
        self._client_email = ""
        # 追加列要用数值 sheetId 而不是标题，由 _ensure_worksheet 填入
        self._sheet_id: Optional[int] = None

    @classmethod
    def validate_options(cls, options: Dict[str, Any]) -> None:
        """必须提供服务账号的 JSON 密钥文件路径。文件是否存在留到推送时检查，以免挂载延迟时整个实例被判为不可用。"""
        if not str((options or {}).get("credentials_file") or "").strip():
            raise DestinationError("缺少 credentials_file（服务账号 JSON 密钥文件路径）")

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
    # HTTP
    # ------------------------------------------------------------------

    def _get_session(self) -> Any:
        """惰性创建带服务账号凭据的会话。"""
        if self._session is not None:
            return self._session
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError:
            raise DestinationError("缺少依赖 google-auth，请执行 pip install -r requirements.txt")

        path = Path(str(self.options.get("credentials_file") or "")).expanduser()
        if not path.is_file():
            raise DestinationError(f"服务账号密钥文件不存在：{path}")
        try:
            credentials = service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
        except (ValueError, OSError) as e:
            raise DestinationError(f"服务账号密钥文件无法解析：{e}")
        self._client_email = getattr(credentials, "service_account_email", "") or ""
        self._session = AuthorizedSession(credentials)
        return self._session

    def _call(self, method: str, url: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
        """发一次请求；429 与 5xx 退避重试，其余错误转成可读的 DestinationError。"""
        session = self._get_session()
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = session.request(
                    method, url, params=params, json=body, timeout=REQUEST_TIMEOUT_SECONDS
                )
            except Exception as e:
                # 网络抖动与 5xx 同等对待：本次推送失败的代价是整张台账晚一周更新
                if attempt < MAX_ATTEMPTS - 1:
                    self._sleep(min(2 ** attempt, 32))
                    continue
                raise DestinationError(f"无法连接 Google Sheets：{e}")

            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
                delay = min(2 ** attempt, 32)
                retry_after = response.headers.get("Retry-After") if response.headers else None
                if retry_after and str(retry_after).isdigit():
                    delay = min(int(retry_after), 60)
                logger.info("Google Sheets %s, retrying in %ss", response.status_code, delay)
                self._sleep(delay)
                continue

            if response.status_code >= 400:
                raise DestinationError(self._describe_error(response))
            if not response.content:
                return {}
            return response.json()
        raise DestinationError("Google Sheets 请求多次重试后仍失败")

    def _describe_error(self, response: Any) -> str:
        """把接口错误翻译成能指导排查的一句话。"""
        message = ""
        try:
            message = response.json().get("error", {}).get("message", "")
        except Exception:
            message = (getattr(response, "text", "") or "")[:300]
        status = response.status_code
        who = f"服务账号 {self._client_email}" if self._client_email else "服务账号"
        if status == 403:
            return f"没有权限写入该表格，请确认已把表格以「编辑者」身份共享给{who}（{message}）"
        if status == 404:
            return f"表格不存在，或{who}无权访问（{message}）"
        return f"Google Sheets 返回 {status}：{message}"

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
        base = f"{API_BASE}/{quote(spreadsheet_id, safe='')}"
        sheet_ref = quote_sheet_title(worksheet)

        grid_columns = self._ensure_worksheet(base, worksheet)
        positions, new_headers = self._resolve_columns(base, sheet_ref, columns)

        needed = max(positions.values()) + 1
        if needed > grid_columns:
            # 写到网格之外会被接口直接拒绝，而不是自动扩列
            self._append_columns(base, worksheet, needed - grid_columns)
        if new_headers:
            self._write_cells(base, [
                {"range": f"{sheet_ref}!{column_letter(index)}1", "values": [[header]]}
                for index, header in new_headers
            ])

        key_column = columns[0]
        if key_column.header in {header for _, header in new_headers}:
            # 键列是刚补出来的，说明表里还没有任何台账行，不必再读
            existing: Dict[str, List[int]] = {}
        else:
            existing = self._read_keys(base, sheet_ref, positions[key_column.key])

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
            self._write_cells(base, updates)
        if appends:
            self._append_rows(base, sheet_ref, appends)
        return stats

    def _ensure_worksheet(self, base: str, worksheet: str) -> int:
        """确保工作表存在，返回它当前的列数。"""
        meta = self._call(
            "GET", base, params={"fields": "sheets.properties(sheetId,title,gridProperties)"}
        )
        for sheet in meta.get("sheets", []) or []:
            props = sheet.get("properties", {}) or {}
            if props.get("title") == worksheet:
                self._sheet_id = props.get("sheetId")
                return int((props.get("gridProperties") or {}).get("columnCount") or 26)

        reply = self._call(
            "POST", f"{base}:batchUpdate",
            body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
        )
        props = ((reply.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        self._sheet_id = props.get("sheetId")
        logger.info("Created worksheet %r in spreadsheet %s", worksheet, base.rsplit("/", 1)[-1])
        return int((props.get("gridProperties") or {}).get("columnCount") or 26)

    def _append_columns(self, base: str, worksheet: str, count: int) -> None:
        """在工作表末尾追加列。"""
        self._call(
            "POST", f"{base}:batchUpdate",
            body={"requests": [{"appendDimension": {
                "sheetId": self._sheet_id, "dimension": "COLUMNS", "length": int(count),
            }}]},
        )

    def _resolve_columns(
        self, base: str, sheet_ref: str, columns: List[Column]
    ) -> Tuple[Dict[str, int], List[Tuple[int, str]]]:
        """
        读表头，按名称定位每个系统列。

        返回 ({column.key: 列序号}, [(列序号, 需要补写的表头)])。
        缺失的系统列补在现有表头之后，不插到中间 —— 插列会挪动用户的人工列。
        """
        data = self._call("GET", f"{base}/values/{quote(sheet_ref + '!1:1', safe='')}")
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

    def _read_keys(self, base: str, sheet_ref: str, key_index: int) -> Dict[str, List[int]]:
        """读键列，返回 {键: [行号...]}。同一个键出现多行（用户复制过行）时每一行都更新。"""
        letter = column_letter(key_index)
        data = self._call(
            "GET",
            f"{base}/values/{quote(f'{sheet_ref}!{letter}2:{letter}', safe='')}",
            params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"},
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

    def _write_cells(self, base: str, data: List[dict]) -> None:
        """
        批量写入若干范围。

        valueInputOption 必须是 RAW：正文来自模型，以 "=" 开头的内容在 USER_ENTERED 下
        会被当成公式执行（例如 =IMPORTXML 把表内数据发往外部地址）。
        """
        for batch in _chunks_by_size(data):
            self._call("POST", f"{base}/values:batchUpdate", body={"valueInputOption": "RAW", "data": batch})

    def _append_rows(self, base: str, sheet_ref: str, rows: List[List[Any]]) -> None:
        """
        在表格末尾追加行。

        人工列位置填 null：追加接口会跳过 null，不会往用户的列里写空串。
        INSERT_ROWS 让接口自动扩行，不受网格行数限制。
        """
        url = f"{base}/values/{quote(sheet_ref + '!A1', safe='')}:append"
        for batch in _chunks_by_size(rows):
            self._call(
                "POST", url,
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                body={"majorDimension": "ROWS", "values": batch},
            )


def _range(sheet_ref: str, row_number: int, start: int, values: List[Any]) -> dict:
    """一段连续单元格的写入范围。"""
    first = column_letter(start)
    last = column_letter(start + len(values) - 1)
    return {"range": f"{sheet_ref}!{first}{row_number}:{last}{row_number}", "values": [values]}
