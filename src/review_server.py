#!/usr/bin/env python3
"""
OpenCR - 自动代码审查服务
优先读取 config.yaml，支持环境变量覆盖，兼容 ~/.codex 回退
"""

import os
import sys
import json
import logging
import subprocess
import re
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict

from flask import Flask, request, jsonify
import openai

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.expanduser('~/opencr/logs/server.log'))
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 尝试导入 tomllib (Python 3.11+) 或 tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        logger.warning("tomllib/tomli not found, using basic config parsing")
        tomllib = None


try:
    import yaml
except ImportError:
    yaml = None


def _parse_yaml_scalar(value: str) -> Any:
    """解析 YAML 标量（简化版，用于无 PyYAML 场景）"""
    raw = value.strip()
    if not raw:
        return ""

    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]

    lower = raw.lower()
    if lower in {"true", "false"}:
        return lower == "true"

    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw

    return raw


def _parse_basic_yaml(content: str) -> Dict[str, Any]:
    """
    简化 YAML 解析（仅支持 key/value 和对象嵌套）。
    当运行环境没有 PyYAML 时作为后备解析器。
    """
    root: Dict[str, Any] = {}
    stack = [(-1, root)]

    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = re.match(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue

        indent = len(match.group(1))
        key = match.group(2)
        raw_value = match.group(3)

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1] if stack else root
        if not isinstance(parent, dict):
            continue

        if raw_value == "":
            nested: Dict[str, Any] = {}
            parent[key] = nested
            stack.append((indent, nested))
        else:
            if " #" in raw_value and not raw_value.strip().startswith(("'", '"')):
                raw_value = raw_value.split(" #", 1)[0].rstrip()
            parent[key] = _parse_yaml_scalar(raw_value)

    return root


def _resolve_config_candidates() -> list:
    module_dir = Path(__file__).resolve().parent
    env_path = os.getenv("OPENCR_CONFIG_PATH", "").strip()
    candidates = []

    if env_path:
        candidates.append(Path(env_path).expanduser())

    candidates.extend([
        Path.cwd() / "config.yaml",
        module_dir.parent / "config.yaml",
        module_dir / "config.yaml",
        Path.home() / "opencr" / "config.yaml",
    ])

    unique_paths = []
    seen = set()
    for path in candidates:
        normalized = str(path.resolve()) if path.exists() else str(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_paths.append(path)
    return unique_paths


def load_file_config() -> dict:
    """加载 config.yaml（优先 OPENCR_CONFIG_PATH）"""
    for config_path in _resolve_config_candidates():
        if not config_path.exists() or not config_path.is_file():
            continue

        try:
            content = config_path.read_text(encoding="utf-8")
            if yaml:
                parsed = yaml.safe_load(content) or {}
            else:
                parsed = _parse_basic_yaml(content)

            if not isinstance(parsed, dict):
                logger.warning(f"config.yaml 内容不是对象结构，忽略: {config_path}")
                return {}

            logger.info(f"Loaded config.yaml: {config_path}")
            return parsed
        except Exception as e:
            logger.error(f"Failed to parse config.yaml ({config_path}): {e}")
            return {}

    return {}


def _pick_config_value(config_data: dict, *paths: str) -> str:
    for path in paths:
        current = config_data
        found = True

        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current.get(key)

        if not found or current is None:
            continue

        if isinstance(current, str):
            value = current.strip()
            if value:
                return value
            continue

        # YAML 中可能为数字/布尔类型，统一转为字符串
        return str(current)

    return ""


def get_app_version() -> str:
    """读取服务版本：固定从 src/__init__.py 获取"""
    init_file = Path(__file__).resolve().parent / "__init__.py"
    try:
        content = init_file.read_text(encoding="utf-8")
        match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', content, flags=re.MULTILINE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    except Exception as e:
        logger.warning(f"Failed to load app version from {init_file}: {e}")

    return "unknown"


def load_openai_config():
    """加载 AI 配置：优先 config.yaml，环境变量覆盖，缺失时回退 ~/.codex"""

    codex_dir = Path.home() / ".codex"
    config = {
        "model": "gpt-4.1",
        "reasoning_effort": "medium",
        "base_url": "https://api.openai.com/v1",
        "api_key": ""
    }
    used_codex_fallback = False
    config_data = load_file_config()
    config_file_values = {
        "model": _pick_config_value(config_data, "openai.model", "OPENAI_MODEL"),
        "reasoning_effort": _pick_config_value(config_data, "openai.reasoning_effort", "OPENAI_REASONING_EFFORT"),
        "base_url": _pick_config_value(config_data, "openai.base_url", "OPENAI_BASE_URL"),
        "api_key": _pick_config_value(config_data, "openai.api_key", "OPENAI_API_KEY"),
    }

    env_values = {
        "model": os.getenv("OPENAI_MODEL", ""),
        "reasoning_effort": os.getenv("OPENAI_REASONING_EFFORT", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", ""),
        "api_key": os.getenv("OPENAI_API_KEY", "")
    }

    for key, value in config_file_values.items():
        if value:
            config[key] = value

    for key, value in env_values.items():
        if value:
            config[key] = value

    missing_fields = [k for k in ("model", "base_url", "api_key") if not config.get(k)]

    # 1. 缺失项时回退读取 config.toml
    config_path = codex_dir / "config.toml"
    if missing_fields and config_path.exists() and tomllib:
        try:
            with open(config_path, "rb") as f:
                toml_config = tomllib.load(f)

            provider_name = toml_config.get("model_provider", "openai")
            provider_config = toml_config.get("model_providers", {}).get(provider_name, {})

            if not env_values["model"]:
                config["model"] = toml_config.get("model", config["model"])
            if not env_values["reasoning_effort"]:
                config["reasoning_effort"] = toml_config.get("model_reasoning_effort", config["reasoning_effort"])
            if not env_values["base_url"]:
                config["base_url"] = provider_config.get("base_url", config["base_url"])

            used_codex_fallback = True
            logger.info(f"Loaded ~/.codex/config.toml fallback: provider={provider_name}, model={config['model']}")
        except Exception as e:
            logger.error(f"Failed to parse config.toml: {e}")

    # 2. 缺失 API Key 时回退读取 auth.json
    auth_path = codex_dir / "auth.json"
    if not config["api_key"] and auth_path.exists():
        try:
            with open(auth_path, "r") as f:
                auth = json.load(f)
            config["api_key"] = auth.get("OPENAI_API_KEY", config["api_key"])
            used_codex_fallback = True
        except Exception as e:
            logger.error(f"Failed to parse auth.json: {e}")

    source_parts = []
    if any(config_file_values.values()):
        source_parts.append("config.yaml")
    if any(env_values.values()):
        source_parts.append("env(override)")
    if used_codex_fallback:
        source_parts.append("~/.codex(fallback)")
    if not source_parts:
        source_parts.append("defaults")

    source = "+".join(source_parts)
    logger.info(f"AI config loaded from {source}: model={config['model']}, base_url={config['base_url']}")

    return config


class ReviewError(Exception):
    """审查过程中的错误"""
    pass


def load_gitlab_config() -> dict:
    """读取 GitLab 配置：优先 config.yaml，环境变量覆盖，兼容新旧命名"""
    config_data = load_file_config()
    gitlab_url = _pick_config_value(
        config_data,
        "code_platform.url",
        "gitlab.url",
        "CODE_PLATFORM_URL",
        "GITLAB_URL",
    ).rstrip("/")
    token = _pick_config_value(
        config_data,
        "code_platform.token",
        "gitlab.token",
        "CODE_PLATFORM_TOKEN",
        "GITLAB_API_TOKEN",
    )
    webhook_secret = _pick_config_value(
        config_data,
        "code_platform.webhook_secret",
        "gitlab.webhook_secret",
        "WEBHOOK_SECRET",
        "GITLAB_WEBHOOK_SECRET",
    )

    env_url = (os.getenv('GITLAB_URL') or os.getenv('CODE_PLATFORM_URL') or '').rstrip('/')
    env_token = os.getenv('GITLAB_API_TOKEN') or os.getenv('CODE_PLATFORM_TOKEN') or ''
    env_webhook_secret = os.getenv('GITLAB_WEBHOOK_SECRET') or os.getenv('WEBHOOK_SECRET') or ''

    if env_url:
        gitlab_url = env_url
    if env_token:
        token = env_token
    if env_webhook_secret:
        webhook_secret = env_webhook_secret

    return {
        'url': gitlab_url,
        'token': token,
        'webhook_secret': webhook_secret
    }


def require_gitlab_config() -> dict:
    """确保 GitLab 基础配置完整"""
    cfg = load_gitlab_config()

    if not cfg['url'] or not cfg['token']:
        logger.error(
            f"GitLab config missing: url={'SET' if cfg['url'] else 'EMPTY'}, "
            f"token={'SET' if cfg['token'] else 'EMPTY'}"
        )
        raise ReviewError(f"GitLab 配置不完整: url={bool(cfg['url'])}, token={bool(cfg['token'])}")

    return cfg


def validate_webhook_token(f):
    """Webhook 安全验证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        secret_token = load_gitlab_config()['webhook_secret']
        if secret_token:
            header_token = request.headers.get('X-Gitlab-Token')
            if header_token != secret_token:
                logger.warning(f"Invalid webhook token")
                return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def truncate_diff(diff: str, max_chars: int = 50000) -> str:
    """智能截断 diff 内容"""
    if len(diff) <= max_chars:
        return diff

    file_pattern = r'diff --git a/(.+?) b/\1'
    files = re.split(file_pattern, diff)

    if len(files) <= 1:
        return diff[:max_chars] + "\n\n... (内容已截断)"

    result = []
    current_size = 0

    for i in range(1, len(files), 2):
        if i >= len(files):
            break

        filename = files[i]
        content = files[i + 1] if i + 1 < len(files) else ""

        file_header = f"diff --git a/{filename} b/{filename}\n"
        file_diff = file_header + content

        if current_size + len(file_diff) > max_chars:
            remaining = max_chars - current_size
            if remaining > 200:
                truncated = file_diff[:remaining] + "\n... (文件内容已截断)\n"
                result.append(truncated)
            break
        else:
            result.append(file_diff)
            current_size += len(file_diff)

    return ''.join(result) + "\n\n... (更多文件未显示)"


def build_review_prompt(diff: str) -> str:
    """构建审查提示词"""
    return f"""你是一位经验丰富的代码审查专家。请对以下代码变更进行审查。

## 审查重点

1. **Bug 与逻辑错误**
   - 空指针/未定义变量
   - 逻辑条件错误
   - 资源泄漏

2. **安全问题**
   - SQL 注入、XSS
   - 敏感信息硬编码
   - 不安全的文件操作

3. **性能问题**
   - 重复计算
   - 不必要的循环
   - 内存泄漏风险

4. **代码质量**
   - 可读性
   - 可维护性

## 输出格式

请按以下 Markdown 格式输出：

### 总体评价
简要评价本次变更的质量

### 详细审查结果
如有问题，按以下格式列出：

#### 问题 1: [标题]
- **位置**: `文件路径:行号`
- **级别**: 🔴 严重 / 🟡 警告 / 🟢 建议
- **描述**: 具体问题
- **建议**: 修复方案

### 如无问题
回复："✅ 代码审查通过，未发现明显问题。"

---

```diff
{diff}
```
"""


def call_codex_review(diff: str) -> str:
    """调用 Codex/OpenAI API 进行代码审查"""

    cfg = load_openai_config()

    if not cfg["api_key"]:
        raise ReviewError("未配置 API Key")

    logger.info(f"Calling API: model={cfg['model']}, base_url={cfg['base_url']}")

    try:
        client = openai.OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"]
        )

        messages = [{
            "role": "user",
            "content": build_review_prompt(diff)
        }]

        kwargs = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": 4000
        }

        # o-series 模型支持 reasoning_effort
        if cfg["reasoning_effort"] and any(x in cfg["model"].lower() for x in ["o3", "o4"]):
            kwargs["reasoning_effort"] = cfg["reasoning_effort"]

        response = client.chat.completions.create(**kwargs)
        logger.debug(f"API raw response: {response}")

        # 处理可能的 None 响应
        if not response.choices or len(response.choices) == 0:
            raise ReviewError("API 返回空 choices")

        message = response.choices[0].message
        content = message.content

        if content is None:
            # 可能是流式响应或思考中的响应
            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                content = message.reasoning_content
            elif hasattr(message, 'text') and message.text:
                content = message.text
            else:
                # 尝试将整个响应转换为字符串
                content = f"API 返回格式异常，原始响应: {json.dumps(response.model_dump(), ensure_ascii=False, default=str)[:2000]}"

        logger.info(f"API response: {len(str(content))} characters")
        return content

    except Exception as e:
        logger.exception("API call failed")
        raise ReviewError(f"API 调用失败: {str(e)}")


def get_mr_diff(project_id: int, mr_iid: int) -> str:
    """从 GitLab API 获取 MR diff"""

    cfg = require_gitlab_config()
    gitlab_url = cfg['url']
    token = cfg['token']

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"

    headers = {
        'PRIVATE-TOKEN': token,
        'Content-Type': 'application/json'
    }

    try:
        import requests
        # 禁用 SSL 验证（用于私有 GitLab 证书）
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(url, headers=headers, timeout=30, verify=False)
        response.raise_for_status()

        data = response.json()
        changes = data.get('changes', [])

        if not changes:
            return ""

        return '\n'.join([c.get('diff', '') for c in changes])

    except Exception as e:
        logger.error(f"Failed to fetch MR diff: {e}")
        raise ReviewError(f"获取 MR diff 失败: {str(e)}")


def post_mr_comment(project_id: int, mr_iid: int, content: str) -> None:
    """在 MR 中发表评论"""

    cfg = require_gitlab_config()
    gitlab_url = cfg['url']
    token = cfg['token']

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"

    headers = {
        'PRIVATE-TOKEN': token,
        'Content-Type': 'application/json'
    }

    cfg = load_openai_config()
    full_content = f"""## 🤖 AI 代码审查报告

**审查时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**审查模型**: {cfg['model']}

---

{content}

"""

    try:
        import requests
        # 禁用 SSL 验证（用于私有 GitLab 证书）
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(url, headers=headers, json={'body': full_content}, timeout=30, verify=False)
        response.raise_for_status()
        logger.info(f"Posted comment to MR !{mr_iid}")

    except Exception as e:
        logger.error(f"Failed to post comment: {e}")
        raise ReviewError(f"发表评论失败: {str(e)}")


def should_review_mr(data: dict) -> tuple:
    """判断是否应该审查此 MR"""
    attrs = data.get('object_attributes', {})

    action = attrs.get('action')
    if action not in ['open', 'reopen']:
        return False, f"忽略 action={action} 的 MR"

    title = attrs.get('title', '').lower()
    skip_keywords = ['wip', 'draft', 'skip-review', '[skip ci]']
    for kw in skip_keywords:
        if kw in title:
            return False, f"标题包含跳过标记: {kw}"

    source_branch = attrs.get('source_branch', '')
    if source_branch.startswith('dependabot/'):
        return False, "跳过依赖更新 MR"

    return True, "符合审查条件"


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    cfg = load_openai_config()
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'version': get_app_version(),
        'model': cfg.get('model', 'unknown'),
        'base_url': cfg.get('base_url', 'unknown')
    })


def process_review_async(project_id, mr_iid, mr_title):
    """后台线程处理审查"""
    try:
        logger.info(f"[Async] Fetching diff for MR !{mr_iid}")
        diff = get_mr_diff(project_id, mr_iid)

        if not diff:
            post_mr_comment(project_id, mr_iid, "⚠️ 无法获取代码变更内容")
            return

        original_size = len(diff)
        diff = truncate_diff(diff)
        if len(diff) < original_size:
            logger.info(f"[Async] Diff truncated: {original_size} -> {len(diff)} chars")

        review_result = call_codex_review(diff)
        post_mr_comment(project_id, mr_iid, review_result)

        logger.info(f"[Async] Successfully reviewed MR !{mr_iid}")

    except ReviewError as e:
        logger.error(f"[Async] Review failed for MR !{mr_iid}: {e}")
        try:
            post_mr_comment(project_id, mr_iid, f"❌ 代码审查失败\n\n```\n{str(e)}\n```")
        except:
            pass
    except Exception as e:
        logger.exception(f"[Async] Unexpected error for MR !{mr_iid}")


@app.route('/webhook', methods=['POST'])
@validate_webhook_token
def handle_webhook():
    """处理 GitLab Webhook - 立即返回，后台处理"""
    data = request.json

    if not data:
        return jsonify({'error': 'No JSON payload'}), 400

    event_type = data.get('object_kind')
    if event_type != 'merge_request':
        return jsonify({'message': f'Ignored event type: {event_type}'}), 200

    attrs = data.get('object_attributes', {})
    project = data.get('project', {})

    project_id = project.get('id')
    mr_iid = attrs.get('iid')
    mr_title = attrs.get('title')

    logger.info(f"Received MR webhook: !{mr_iid} - {mr_title}")

    should_review, reason = should_review_mr(data)
    if not should_review:
        logger.info(f"Skipping MR !{mr_iid}: {reason}")
        return jsonify({'message': f'Skipped: {reason}'}), 200

    # 启动后台线程处理审查，立即返回 202 Accepted
    thread = threading.Thread(
        target=process_review_async,
        args=(project_id, mr_iid, mr_title)
    )
    thread.daemon = True
    thread.start()

    logger.info(f"Started async review for MR !{mr_iid}")
    return jsonify({
        'message': 'Review started',
        'mr_iid': mr_iid,
        'status': 'processing'
    }), 202


@app.route('/manual-review', methods=['POST'])
def manual_review():
    """手动触发审查"""
    data = request.json
    project_id = data.get('project_id')
    mr_iid = data.get('mr_iid')

    if not project_id or not mr_iid:
        return jsonify({'error': 'Missing project_id or mr_iid'}), 400

    logger.info(f"Manual review requested for !{mr_iid}")

    try:
        diff = get_mr_diff(project_id, mr_iid)
        review_result = call_codex_review(truncate_diff(diff))
        post_mr_comment(project_id, mr_iid, review_result)
        return jsonify({'message': 'Manual review completed'}), 200

    except Exception as e:
        logger.exception("Manual review failed")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    config_data = load_file_config()
    app_version = get_app_version()

    host = os.getenv('REVIEW_SERVER_HOST') or _pick_config_value(config_data, "server.host", "REVIEW_SERVER_HOST") or '0.0.0.0'
    port_value = os.getenv('REVIEW_SERVER_PORT') or _pick_config_value(config_data, "server.port", "REVIEW_SERVER_PORT") or '5000'
    try:
        port = int(port_value)
    except ValueError:
        logger.warning(f"Invalid REVIEW_SERVER_PORT={port_value}, fallback to 5000")
        port = 5000

    cfg = load_openai_config()
    logger.info(f"Starting OpenCR v{app_version} on {host}:{port}")
    logger.info(f"Model: {cfg['model']}, Base URL: {cfg['base_url']}")

    # 检查 GitLab 配置
    gitlab_cfg = load_gitlab_config()
    gitlab_url = gitlab_cfg['url']
    gitlab_token = gitlab_cfg['token']
    logger.info(f"GitLab URL: {gitlab_url[:30] if gitlab_url else 'NOT SET'}...")
    logger.info(f"GitLab Token: {'SET' if gitlab_token else 'NOT SET'}")

    app.run(host=host, port=port, debug=False)
