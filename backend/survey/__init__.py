#!/usr/bin/env python3
"""
定期巡检（Survey）。

与 MR 审查（backend/review/）是两条彼此独立的链路：
后者由 webhook 事件驱动、审查一次变更；本包由时间驱动、审查整组仓库的全量代码。
术语以 CONTEXT.md 的「定期巡检」一节为准。
"""
