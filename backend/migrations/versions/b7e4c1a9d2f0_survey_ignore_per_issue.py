"""survey ignore per issue

Revision ID: b7e4c1a9d2f0
Revises: d2a3378d6cb3
Create Date: 2026-10-10 18:00:00.000000
"""
import json
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e4c1a9d2f0'
down_revision: Union[str, None] = 'd2a3378d6cb3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_title(title) -> str:
    """
    标题的比对形式。

    刻意内联而不是从 backend.survey.common 导入：迁移脚本要永远按写下时的逻辑执行，
    应用代码以后改了规整规则，不能回头改变一次已经跑过的迁移的结果。
    """
    return re.sub(r"\s+", " ", str(title or "")).strip().lower()


def _snapshot_title(finding) -> str:
    """
    快照标题：发现的标题，空时退回正文第一行，再不行用位置。

    与 backend/storage/repo.py 的 _ignore_snapshot_title 同一规则，理由同 _normalize_title 要内联。
    标题空着的话，换算出来的忽略从迁移起就不生效。
    """
    title = (finding["title"] or "").strip()
    if title:
        return title
    body = (finding["body"] or "").strip()
    if body:
        return body.splitlines()[0][:120]
    return f"{finding['file_path'] or ''} 第 {finding['line'] or 0} 行的问题"


def upgrade() -> None:
    """
    忽略的单位从指纹改为问题（ADR-0006），并把已有的指纹级忽略换算成问题级。

    换算规则：每条旧忽略找到它的指纹最近一次出现的运行，那次运行里每个不同标题各建一条已忽略问题，
    并关联所有运行里同指纹、标题规整后相同的发现（与标记时的规则一致）。旧语义屏蔽的是整个指纹，
    换算后的范围只会更窄、不会更宽；更早运行里标题不同的同指纹发现不再被隐藏 —— 它们未必是用户想忽略的那个问题。
    找不到发现时退回台账行的快照；连台账行都没有的保留原样（title 为 NULL），它无法交给模型比对、不再生效，
    留着是为了让管理员在忽略清单里看到并自行删除，而不是在升级时静默丢掉。
    """
    with op.batch_alter_table('survey_ignore', schema=None) as batch_op:
        batch_op.add_column(sa.Column('repo_slug', sa.String(length=128), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('file_path', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('category', sa.String(length=24), nullable=False, server_default='correctness'))
        batch_op.add_column(sa.Column('line', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('severity', sa.String(length=16), nullable=False, server_default='unknown'))
        batch_op.add_column(sa.Column('title', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('body', sa.Text(), nullable=True))
        batch_op.drop_index('ux_survey_ignore')
        batch_op.create_index('ix_survey_ignore_survey_fp', ['survey_id', 'fingerprint'], unique=False)

    with op.batch_alter_table('survey_finding', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ignore_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_survey_finding_ignore', ['ignore_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_survey_finding_ignore', 'survey_ignore', ['ignore_id'], ['id'], ondelete='SET NULL'
        )

    _convert_fingerprint_ignores(op.get_bind())


def _convert_fingerprint_ignores(conn) -> None:
    """把升级前的每条指纹级忽略换算成问题级，规则见 upgrade 的 docstring。"""
    legacy = conn.execute(sa.text(
        "SELECT id, survey_id, fingerprint, note, created_at FROM survey_ignore WHERE title IS NULL"
    )).mappings().all()
    insert = sa.text(
        "INSERT INTO survey_ignore (survey_id, fingerprint, repo_slug, file_path, category, line, severity,"
        " title, body, note, created_at) VALUES (:survey_id, :fingerprint, :repo_slug, :file_path, :category,"
        " :line, :severity, :title, :body, :note, :created_at)"
    )
    for row in legacy:
        base = {"survey_id": row["survey_id"], "fingerprint": row["fingerprint"],
                "note": row["note"], "created_at": row["created_at"]}
        run_id = conn.execute(sa.text(
            "SELECT MAX(run_id) FROM survey_finding WHERE survey_id = :s AND fingerprint = :f"
        ), {"s": row["survey_id"], "f": row["fingerprint"]}).scalar()
        if run_id is not None:
            findings = conn.execute(sa.text(
                "SELECT id, run_id, repo_slug, file_path, category, line, severity, title, body FROM survey_finding"
                " WHERE survey_id = :s AND fingerprint = :f ORDER BY id"
            ), {"s": row["survey_id"], "f": row["fingerprint"]}).mappings().all()
            groups = {}
            for finding in findings:
                groups.setdefault(_normalize_title(finding["title"]), []).append(finding)
            latest_titles = {_normalize_title(f["title"]) for f in findings if f["run_id"] == run_id}
            for title_key, items in groups.items():
                if title_key not in latest_titles:
                    continue
                # 快照取最近一次运行里的那条：交给模型比对的应当是用户点"不再提醒"时看到的写法
                first = next(f for f in items if f["run_id"] == run_id)
                conn.execute(insert, {
                    **base, "repo_slug": first["repo_slug"] or "", "file_path": first["file_path"],
                    "category": first["category"], "line": first["line"] or 0,
                    "severity": first["severity"], "title": _snapshot_title(first), "body": first["body"],
                })
                new_id = conn.execute(sa.text("SELECT MAX(id) FROM survey_ignore")).scalar()
                for finding in items:
                    conn.execute(sa.text("UPDATE survey_finding SET ignore_id = :i WHERE id = :id"),
                                 {"i": new_id, "id": finding["id"]})
            conn.execute(sa.text("DELETE FROM survey_ignore WHERE id = :id"), {"id": row["id"]})
            continue

        entry = conn.execute(sa.text(
            "SELECT repo_slug, file_path, category, severity, title, body, lines FROM survey_ledger"
            " WHERE survey_id = :s AND fingerprint = :f"
        ), {"s": row["survey_id"], "f": row["fingerprint"]}).mappings().first()
        if entry is None or not entry["title"]:
            continue
        try:
            lines = [int(n) for n in json.loads(entry["lines"] or "[]") if int(n) > 0]
        except (TypeError, ValueError):
            # 行号只是给模型的定位提示，坏数据按"未定位到行"处理，不值得让升级失败
            lines = []
        conn.execute(insert, {
            **base, "repo_slug": entry["repo_slug"] or "", "file_path": entry["file_path"],
            "category": entry["category"], "line": lines[0] if lines else 0,
            "severity": entry["severity"], "title": entry["title"], "body": entry["body"],
        })
        conn.execute(sa.text("DELETE FROM survey_ignore WHERE id = :id"), {"id": row["id"]})


def downgrade() -> None:
    """
    退回指纹级忽略：同一指纹下的多条已忽略问题合并成一条（保留最早的那条），再删掉问题级的列。

    合并会扩大忽略范围（整个指纹都被屏蔽），这是旧版本唯一能表达的语义。
    """
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM survey_ignore WHERE id NOT IN"
        " (SELECT MIN(id) FROM survey_ignore GROUP BY survey_id, fingerprint)"
    ))

    with op.batch_alter_table('survey_finding', schema=None) as batch_op:
        batch_op.drop_constraint('fk_survey_finding_ignore', type_='foreignkey')
        batch_op.drop_index('ix_survey_finding_ignore')
        batch_op.drop_column('ignore_id')

    with op.batch_alter_table('survey_ignore', schema=None) as batch_op:
        batch_op.drop_index('ix_survey_ignore_survey_fp')
        batch_op.create_index('ux_survey_ignore', ['survey_id', 'fingerprint'], unique=True)
        batch_op.drop_column('body')
        batch_op.drop_column('title')
        batch_op.drop_column('severity')
        batch_op.drop_column('line')
        batch_op.drop_column('category')
        batch_op.drop_column('file_path')
        batch_op.drop_column('repo_slug')
