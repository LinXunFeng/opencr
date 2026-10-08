#!/usr/bin/env python3
"""
部署时检查 gogcli 授权状态的命令行入口，供 install.sh 与 docker-entrypoint.sh 调用。

    python3 -m backend.survey.destinations.gogcli_status             # 逐个实例打印检查结果
    python3 -m backend.survey.destinations.gogcli_status --accounts  # 只列出配置里用到的账号
    python3 -m backend.survey.destinations.gogcli_status --needs-auth  # 只列出需要 `gog auth add` 的账号

检查项与后台「测试连通性」的前两项是同一份实现（GogcliTransport.preflight），
部署脚本不在 shell 里另写一套 `gog auth list` 的解析——两处判定迟早会漂移。

退出码：0 表示没有 gogcli 实例或全部通过；1 表示至少一个实例有问题。
"""

import argparse
import logging
import sys
from typing import Dict, List, Tuple

from . import load_destinations
from .base import CHECK_ERROR, CHECK_OK, CheckItem
from .google_sheet import AUTH_GOGCLI, GoogleSheetDestination
from .google_transport import GogcliTransport

_MARKS = {CHECK_OK: "✓", CHECK_ERROR: "✗"}


def _gogcli_instances() -> List[Tuple[str, Dict, str]]:
    """配置里所有 auth: gogcli 的 Google Sheet 实例，返回 (实例名, options, 配置错误)。"""
    result = []
    for name, info in load_destinations().items():
        if info.type_name != GoogleSheetDestination.type_name:
            continue
        if str(info.options.get("auth") or "").strip() != AUTH_GOGCLI:
            continue
        result.append((name, info.options, info.error or ""))
    return result


def _transport(options: Dict) -> GogcliTransport:
    """按实例配置构造传输层；与 GoogleSheetDestination 的取值方式保持一致。"""
    return GogcliTransport(
        account=str(options.get("account") or "").strip(),
        binary=str(options.get("gogcli_bin") or "gog").strip(),
    )


def _accounts(instances: List[Tuple[str, Dict, str]]) -> List[str]:
    """去重后的账号列表，保持配置中的顺序；配置有误的实例不计入。"""
    seen: Dict[str, str] = {}
    for _, options, error in instances:
        account = str(options.get("account") or "").strip()
        if account and not error:
            seen.setdefault(account.lower(), account)
    return list(seen.values())


def _needs_auth(instances: List[Tuple[str, Dict, str]]) -> List[str]:
    """授权缺失、能靠 `gog auth add` 补上的账号。"""
    result: List[str] = []
    checked: Dict[Tuple[str, str], bool] = {}
    for _, options, error in instances:
        if error:
            continue
        transport = _transport(options)
        key = (transport.account.lower(), transport.binary)
        if key not in checked:
            checked[key] = transport.check_account()[1]
            if checked[key] and transport.account not in result:
                result.append(transport.account)
    return result


def _report(instances: List[Tuple[str, Dict, str]]) -> bool:
    """逐个实例打印检查结果，返回是否全部通过。"""
    ok = True
    # 多个实例共用同一账号很常见（一张表一个实例），每个账号只调一次 gogcli
    cache: Dict[Tuple[str, str], List[CheckItem]] = {}
    for name, options, error in instances:
        account = str(options.get("account") or "").strip() or "（未填写账号）"
        print(f"[gogcli] {name}（{account}）")
        if error:
            print(f"  ✗ 配置有误：{error}")
            ok = False
            continue
        transport = _transport(options)
        key = (transport.account.lower(), transport.binary)
        if key not in cache:
            cache[key] = transport.preflight()
        for item in cache[key]:
            print(f"  {_MARKS.get(item.level, '!')} {item.title}：{item.message}")
            if item.level == CHECK_ERROR:
                ok = False
    return ok


def main(argv: List[str] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="检查 config.yaml 中 auth: gogcli 实例的授权状态")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--accounts", action="store_true", help="只列出配置中用到的账号，每行一个")
    mode.add_argument("--needs-auth", action="store_true", help="只列出需要执行 gog auth add 的账号，每行一个")
    args = parser.parse_args(argv)

    # 配置加载会打 INFO 日志；这里的输出要被 shell 逐行读取，只放过警告
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stderr)

    instances = _gogcli_instances()
    if args.accounts or args.needs_auth:
        # 空列表不输出任何内容：shell 用 `while read` 逐行读，空串会被当成一个空账号
        for account in (_accounts(instances) if args.accounts else _needs_auth(instances)):
            print(account)
        return 0
    return 0 if _report(instances) else 1


if __name__ == "__main__":
    sys.exit(main())
