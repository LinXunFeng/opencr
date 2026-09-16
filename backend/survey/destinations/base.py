#!/usr/bin/env python3
"""
Destination（输出目标）插件的接口定义。

接口刻意划在"按键 upsert"这一层：核心层（survey/ledger.py）算好完整的目标行集，
每列带逻辑类型；插件只负责把行写到平台上，并把逻辑类型映射成平台字段类型。
反过来让插件自己读 Finding、自己判定状态的话，每接一个平台就要重写一遍
LedgerState 的判定，而那恰恰是最容易写错、写错了又看不出来的部分（见 ADR-0004）。

评估一个新平台能不能接入，只需回答三个问题：
能否按键查找已有行、能否批量写入、限流有多严。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Sequence, Tuple

# --- Column.kind：逻辑列类型 --------------------------------------------
# 平台各自映射：Google Sheet 一律写成纯文本/数字；飞书多维表格可以把
# single_select 映射成原生单选、datetime 映射成日期字段。
COLUMN_TEXT = "text"
COLUMN_LONG_TEXT = "long_text"
COLUMN_SINGLE_SELECT = "single_select"
COLUMN_DATETIME = "datetime"
COLUMN_NUMBER = "number"
COLUMN_URL = "url"

# --- CheckItem.level ----------------------------------------------------
CHECK_OK = "ok"
# warn 表示"能用，但有需要知道的事"，例如工作表不存在、首次推送时会自动创建
CHECK_WARN = "warn"
CHECK_ERROR = "error"


class DestinationError(Exception):
    """推送过程中的可预期错误。消息会原样展示给 Admin，措辞要能直接指导排查。"""


@dataclass(frozen=True)
class TargetField:
    """Binding 目标位置里的一个字段，由 Destination 类型声明，前端据此渲染表单。"""

    key: str
    label: str
    required: bool = True
    placeholder: str = ""
    help: str = ""

    def to_dict(self) -> dict:
        """接口数据。"""
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
        }


@dataclass(frozen=True)
class Column:
    """
    Ledger 的一个系统列。

    插件**按 header 定位列**而不是按位置：用户可以在表里随意调整列序、插入人工列。
    代价是系统列的表头不能改名 —— 改名后下次推送会补出一个新列，旧列变成人工列。
    """

    key: str
    header: str
    kind: str = COLUMN_TEXT
    options: Tuple[str, ...] = ()


@dataclass
class LedgerRow:
    """
    一行 Ledger 的目标内容。

    restore_if_missing：平台上找不到这一行时要不要补写。由核心层决定而不是插件 ——
    "用户删掉的行补不补回"是台账语义，不是平台能力。
    """

    key: str
    values: Dict[str, Any]
    restore_if_missing: bool = True


@dataclass
class PushStats:
    """一次推送的写入统计。skipped 指平台上已缺失、且按规则不再补回的行。"""

    updated: int = 0
    inserted: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        """接口数据。"""
        return {"updated": self.updated, "inserted": self.inserted, "skipped": self.skipped}


@dataclass
class CheckItem:
    """连通性测试的一项结论。message 直接展示给 Admin，要能指导下一步操作。"""

    title: str
    level: str
    message: str

    def to_dict(self) -> dict:
        """接口数据。"""
        return {"title": self.title, "level": self.level, "message": self.message}


class Destination(ABC):
    """
    一种 Destination 类型的实现。实例对应 config.yaml 里 `destinations` 下的一项。

    子类需要声明 type_name / label / target_fields，并实现 push。
    """

    type_name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    target_fields: ClassVar[Tuple[TargetField, ...]] = ()

    def __init__(self, name: str, options: Dict[str, Any]):
        """name 是实例名；options 是 config.yaml 里该实例除 type 以外的配置项。"""
        self.validate_options(options)
        self.name = name
        self.options = dict(options)

    @classmethod
    def validate_options(cls, options: Dict[str, Any]) -> None:
        """校验实例配置，非法时抛 DestinationError。默认不校验。"""

    @classmethod
    def normalize_target(cls, target: Dict[str, Any], survey_name: str) -> Dict[str, str]:
        """
        校验并规整 Binding 的目标位置，非法时抛 ValueError。

        默认实现只按 target_fields 取值并检查必填；需要解析链接、补默认值的类型自行覆盖。
        """
        raw = target if isinstance(target, dict) else {}
        normalized: Dict[str, str] = {}
        for item in cls.target_fields:
            value = str(raw.get(item.key) or "").strip()
            if item.required and not value:
                raise ValueError(f"{item.label}不能为空")
            normalized[item.key] = value
        return normalized

    @classmethod
    def describe_options(cls, options: Dict[str, Any]) -> str:
        """实例配置的一行摘要（例如鉴权方式），用于后台下拉框。不得包含任何凭据内容。"""
        return ""

    @classmethod
    def describe_target(cls, target: Dict[str, Any]) -> str:
        """目标位置的一行摘要，用于后台展示。"""
        parts = []
        for item in cls.target_fields:
            value = str((target or {}).get(item.key) or "").strip()
            if value:
                parts.append(f"{item.label}：{value}")
        return " / ".join(parts)

    def check(self, target: Dict[str, str], columns: Sequence[Column]) -> List[CheckItem]:
        """
        连通性测试：凭据能否加载、目标位置能否读写。**不得写入任何台账行。**

        遇到第一个致命问题就返回，后续检查依赖前面的结论，继续做只会堆出一串同源的报错。
        可选实现：不支持的类型返回一条 warn，推送本身不受影响。
        """
        return [CheckItem("连通性测试", CHECK_WARN, f"{self.label or self.type_name} 暂不支持连通性测试")]

    @abstractmethod
    def push(self, target: Dict[str, str], columns: Sequence[Column], rows: List[LedgerRow]) -> PushStats:
        """
        把 rows 按 key 镜像到 target。

        约定：
        - 平台上已存在的行（按 columns[0] 即键列匹配）只覆盖系统列，其余列一个字都不碰；
        - 不存在的行仅在 restore_if_missing 为真时追加；
        - 平台上多出来的行（台账里没有的键）不删除。
        """


@dataclass
class DestinationInfo:
    """config.yaml 里一个 Destination 实例的解析结果。error 非空表示配置有误、不可用。"""

    name: str
    type_name: str
    options: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
