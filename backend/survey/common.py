#!/usr/bin/env python3
"""
巡检的公共常量、异常与纯函数工具。

这里只放不碰网络、不碰数据库的东西，便于单测直接调用。
"""

import hashlib
import re
import unicodedata
from typing import List, Optional

from ..storage.models import SURVEY_CATEGORIES, CATEGORY_CORRECTNESS

# 组织展开出来的仓库上限。没有它的话，指向一个上千项目的顶层 group
# 会让一次巡检把磁盘塞满，而用户在界面上只填了一行。
MAX_REPOS_PER_SURVEY = 50

# 单个仓库工作区的目录名长度上限，留足给文件系统路径
MAX_SLUG_LENGTH = 64

# codegraph 索引在仓库内的目录名。
#
# 它**必须**同时被 workspace 的 `git clean -e` 与 profile 的读取路径引用：
# 两处写死成不同的值不会报错，只会让每轮拉取都把索引删掉，
# 表现为"增量索引好像没生效"，而日志里一切正常。
# 实测这个位置改不了：CODEGRAPH_DIR 只接受相对名，给绝对路径会被静默忽略。
CODEGRAPH_INDEX_DIRNAME = ".codegraph"


class SurveyError(Exception):
    """巡检执行过程中的可预期错误。"""


def slugify(text: str, fallback: str = "repo") -> str:
    """
    把任意名称转成可安全用作目录名的 slug。

    必须做这一步的原因不是美观：name 是用户自由输入的，中文、空格、以及
    "../" 都会直接变成文件系统路径的一部分。这里只放行 [a-z0-9-]。
    """
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        # 中文名会在上面被整段丢掉，退回哈希而不是报错 —— 用中文命名巡检很正常
        digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]
        slug = f"{fallback}-{digest}"
    return slug[:MAX_SLUG_LENGTH].strip("-")


def repo_slug_from_url(url: str) -> str:
    """
    从仓库地址推出工作区目录名。

    取最后两段路径（group/project），避免不同 group 下的同名项目撞目录。
    """
    cleaned = str(url or "").strip().rstrip("/")
    cleaned = re.sub(r"\.git$", "", cleaned)
    # 去掉协议与可能内嵌的凭据，只留路径部分
    cleaned = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", cleaned)
    cleaned = re.sub(r"^[^/@]*@", "", cleaned)
    cleaned = cleaned.replace(":", "/")
    parts = [p for p in cleaned.split("/") if p]
    tail = parts[-2:] if len(parts) >= 2 else parts[-1:]
    return slugify("-".join(tail)) or "repo"


def normalize_category(value: str) -> str:
    """
    把模型输出的类别规整到闭集内。

    不做模糊匹配兜底：类别是指纹的组成部分，猜错一次就会让同一条问题在
    下一轮被判成"新增"，比直接落到默认档更糟。
    """
    raw = str(value or "").strip().lower().replace("-", "_")
    return raw if raw in SURVEY_CATEGORIES else CATEGORY_CORRECTNESS


def finding_fingerprint(repo_slug: str, file_path: str, category: str) -> str:
    """
    Finding 的跨轮次身份。

    刻意**不含正文**：模型两次描述同一个问题的措辞不会一样，
    把正文纳入哈希等于每一轮的产出全是"新增"，对比机制直接失效。
    """
    payload = "\n".join(
        [
            str(repo_slug or "").strip(),
            str(file_path or "").strip(),
            normalize_category(category),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_json_list(raw: Optional[str]) -> List[str]:
    """解析 JSON 数组字段；脏数据退化为空列表而不是抛错。"""
    import json

    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return [str(i) for i in items if str(i).strip()]


def matches_any_pattern(text: str, patterns: List[str]) -> bool:
    """
    仓库路径是否命中任一排除模式。

    先按正则试，正则非法时退回子串匹配 —— 用户在界面上填 "legacy" 这种
    朴素关键字的概率远高于填正则，不该因为它不是合法正则就整条失效。
    """
    target = str(text or "")
    for pattern in patterns or []:
        p = str(pattern or "").strip()
        if not p:
            continue
        try:
            if re.search(p, target):
                return True
        except re.error:
            if p in target:
                return True
    return False
