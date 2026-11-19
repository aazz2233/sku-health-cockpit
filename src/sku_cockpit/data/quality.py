"""Data quality report: counts, reject reasons, dirty-row recall."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, func, select

from sku_cockpit.config import Settings, get_settings
from sku_cockpit.data.ingest import IngestReport
from sku_cockpit.schema import (
    STAGING_TABLES,
    anomaly_label,
    ingest_run,
    raw_row,
    reject_row,
)


@dataclass
class DirtyMatch:
    file: str
    expected: int
    matched: int
    missed: list[dict[str, object]]

    @property
    def recall(self) -> float:
        return 1.0 if self.expected == 0 else self.matched / self.expected


def staging_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name, table in STAGING_TABLES.items():
            counts[name] = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
        counts["raw_row"] = int(
            conn.execute(select(func.count()).select_from(raw_row)).scalar_one()
        )
        counts["reject_row"] = int(
            conn.execute(select(func.count()).select_from(reject_row)).scalar_one()
        )
        counts["anomaly_label"] = int(
            conn.execute(select(func.count()).select_from(anomaly_label)).scalar_one()
        )
    return counts


def reject_breakdown(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(reject_row.c.reason_code, func.count())
            .group_by(reject_row.c.reason_code)
            .order_by(func.count().desc())
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def compare_with_manifest(engine: Engine, manifest_path: Path) -> list[DirtyMatch]:
    """Recall of injected dirty rows: manifest says X, did we reject exactly X?"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: list[DirtyMatch] = []
    with engine.connect() as conn:
        for entry in manifest["files"]:
            expected = [d for d in entry.get("dirty", []) if d.get("expect") == "rejected"]
            if not expected:
                continue
            run = conn.execute(
                select(ingest_run.c.run_id).where(ingest_run.c.source_file == entry["file"])
            ).first()
            actual: set[tuple[int, str]] = set()
            if run is not None:
                rows = conn.execute(
                    select(reject_row.c.row_number, reject_row.c.reason_code).where(
                        reject_row.c.run_id == run[0]
                    )
                ).all()
                actual = {(int(row[0]), str(row[1])) for row in rows}
            matched = 0
            missed: list[dict[str, object]] = []
            for expected_row in expected:
                key = (int(expected_row["row"]), str(expected_row["reason_code"]))
                if key in actual:
                    matched += 1
                else:
                    missed.append(expected_row)
            results.append(
                DirtyMatch(
                    file=str(entry["file"]),
                    expected=len(expected),
                    matched=matched,
                    missed=missed,
                )
            )
    return results


def write_report(
    engine: Engine,
    report: IngestReport,
    manifest_path: Path,
    out_path: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or get_settings()
    counts = staging_counts(engine)
    reasons = reject_breakdown(engine)
    dirty = compare_with_manifest(engine, manifest_path)
    totals = report.totals

    lines: list[str] = []
    lines.append("# 数据质量报告\n")
    lines.append(f"- 数据种子：`{settings.data_seed}`")
    lines.append(f"- 生成目录：`{manifest_path.parent}`")
    lines.append("")

    lines.append("## 各表行数\n")
    lines.append("| 表 | 行数 |")
    lines.append("| --- | ---: |")
    for name, value in counts.items():
        lines.append(f"| `{name}` | {value:,} |")
    lines.append("")

    if totals["files"]:
        lines.append("## 本次导入\n")
        lines.append("| 指标 | 值 |")
        lines.append("| --- | ---: |")
        for key, value in totals.items():
            lines.append(f"| {key} | {value:,} |")
        lines.append("")

    lines.append("## 拒绝原因分布\n")
    if reasons:
        lines.append("| 原因码 | 行数 |")
        lines.append("| --- | ---: |")
        for reason, value in reasons.items():
            lines.append(f"| `{reason}` | {value:,} |")
    else:
        lines.append("_没有拒绝行_")
    lines.append("")

    lines.append("## 脏数据召回（对照 manifest）\n")
    if dirty:
        lines.append("| 文件 | 期望拒绝 | 命中 | 召回率 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for item in dirty:
            lines.append(
                f"| `{item.file}` | {item.expected} | {item.matched} | {item.recall:.0%} |"
            )
        missed_rows: list[tuple[str, dict[str, object]]] = [
            (match.file, row) for match in dirty for row in match.missed
        ]
        if missed_rows:
            lines.append("\n**未命中：**\n")
            for file_name, row in missed_rows:
                lines.append(
                    f"- `{file_name}` 第 {row['row']} 行：期望 `{row['reason_code']}`（{row['detail']}）"
                )
    else:
        lines.append("_manifest 中没有需要拒绝的脏数据_")
    lines.append("")

    lines.append("## 设计取舍\n")
    lines.append(
        "- `raw_row` 只保留 **orders / refunds** 的完整原始载荷；其余数据集只保留被拒行，"
        "通过 `staging_*.source_run_id + source_row` 回溯。这是存储量与可审计性的折中。"
    )
    lines.append("- D7（脏表头）不产生拒绝，而是被映射层的表头归一化吸收，因此按「absorbed」单列。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
