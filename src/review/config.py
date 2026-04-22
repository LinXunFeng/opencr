#!/usr/bin/env python3
"""
配置加载与解析
"""

import json
import logging
import os
import re
from pathlib import Path

from ..utils.config_utils import (
    parse_basic_yaml as _parse_basic_yaml,
    parse_yaml_scalar as _parse_yaml_scalar,
    pick_config_int as _pick_config_int,
    pick_config_value as _pick_config_value,
)
from .common import (
    DEFAULT_REVIEW_SKILLS_DIR,
)

logger = logging.getLogger(__name__)

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


def _resolve_config_candidates() -> list:
    # 当前文件位于 src/review/config.py，module_dir 指向 src
    module_dir = Path(__file__).resolve().parent.parent
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


def _pick_config_int(config_data: dict, default: int, *paths: str) -> int:
    raw = _pick_config_value(config_data, *paths)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_app_version() -> str:
    """读取服务版本：固定从 src/__init__.py 获取"""
    init_file = Path(__file__).resolve().parent.parent / "__init__.py"
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
        "api_key": "",
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
        "api_key": os.getenv("OPENAI_API_KEY", ""),
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

    env_url = (os.getenv("GITLAB_URL") or os.getenv("CODE_PLATFORM_URL") or "").rstrip("/")
    env_token = os.getenv("GITLAB_API_TOKEN") or os.getenv("CODE_PLATFORM_TOKEN") or ""
    env_webhook_secret = os.getenv("GITLAB_WEBHOOK_SECRET") or os.getenv("WEBHOOK_SECRET") or ""

    if env_url:
        gitlab_url = env_url
    if env_token:
        token = env_token
    if env_webhook_secret:
        webhook_secret = env_webhook_secret

    return {
        "url": gitlab_url,
        "token": token,
        "webhook_secret": webhook_secret,
    }


def load_review_config() -> dict:
    """读取审查配置：默认值 + config.yaml + 环境变量覆盖"""
    config_data = load_file_config()

    skills_dir = _pick_config_value(config_data, "review.skills_dir", "REVIEW_SKILLS_DIR")
    max_diff_size = _pick_config_int(config_data, 50000, "review.max_diff_size", "REVIEW_MAX_DIFF_SIZE")
    timeout = _pick_config_int(config_data, 180, "review.timeout", "REVIEW_TIMEOUT")

    env_skills_dir = os.getenv("REVIEW_SKILLS_DIR", "").strip()
    env_max_diff_size = os.getenv("REVIEW_MAX_DIFF_SIZE", "").strip()
    env_timeout = os.getenv("REVIEW_TIMEOUT", "").strip()

    if env_skills_dir:
        skills_dir = env_skills_dir
    if env_max_diff_size:
        try:
            max_diff_size = int(env_max_diff_size)
        except ValueError:
            logger.warning(f"Invalid REVIEW_MAX_DIFF_SIZE={env_max_diff_size}, fallback to {max_diff_size}")
    if env_timeout:
        try:
            timeout = int(env_timeout)
        except ValueError:
            logger.warning(f"Invalid REVIEW_TIMEOUT={env_timeout}, fallback to {timeout}")

    resolved = {
        "skills_dir": skills_dir or DEFAULT_REVIEW_SKILLS_DIR,
        "max_diff_size": max_diff_size,
        "timeout": timeout,
    }
    logger.info(
        "Review config resolved: skills_dir=%s, max_diff_size=%s, timeout=%s",
        resolved["skills_dir"],
        resolved["max_diff_size"],
        resolved["timeout"],
    )
    return resolved
