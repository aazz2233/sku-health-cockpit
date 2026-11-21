"""Daily operating report: Markdown summary + Excel appendix.

Everything is derived from the mart/diagnosis tables, so the report can never
disagree with the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from sku_cockpit.config import Settings, get_settings


@dataclass
class DailyReport:
    on_date: date
    kpi: dict[str, float]
    prev_kpi: dict[str, float]
    top_anomalies: pd.DataFrame
    pending_reviews: pd.DataFrame
    loss_skus: pd.DataFrame
    stock_risks: int
    markdown: str


def _kpi(engine: Engine, day: date) -> dict[str, float]:
    sql = """
        SELECT COALESCE(SUM(gmv_cny), 0) gmv,
               COALESCE(SUM(net_revenue_cny), 0) revenue,
               COALESCE(SUM(net_profit_cny), 0) profit,
               COALESCE(SUM(orders), 0) orders,
               COALESCE(SUM(units), 0) units,
               COALESCE(SUM(ad_spend_cny), 0) ads
        FROM mart_sku_daily WHERE date_local = :d
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"d": day}).mappings().one()
    kpi = {k: float(v or 0) for k, v in row.items()}
    kpi["margin"] = kpi["profit"] / kpi["revenue"] if kpi["revenue"] else 0.0
    kpi["tacos"] = kpi["ads"] / kpi["revenue"] if kpi["revenue"] else 0.0
    return kpi


def _top_anomalies(engine: Engine, day: date, limit: int = 10) -> pd.DataFrame:
    sql = """
        SELECT e.rule_id, e.severity, e.sku_id, e.shop_id, e.date_local, e.evidence,
               ABS(COALESCE((
                   SELECT SUM(m.net_profit_cny) FROM mart_sku_daily m
                   WHERE m.sku_id = e.sku_id AND m.shop_id = e.shop_id
                     AND m.date_local BETWEEN DATE_SUB(e.date_local, INTERVAL 6 DAY)
                                          AND e.date_local
               ), 0)) AS impact
        FROM anomaly_event e
        WHERE e.date_local = :d AND e.severity IN ('P0', 'P1')
        ORDER BY impact DESC, e.rule_id
        LIMIT :lim
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params={"d": day, "lim": limit})


def _pending_reviews(engine: Engine, limit: int = 10) -> pd.DataFrame:
    sql = """
        SELECT d.sku_id, d.shop_id, d.rule_id, d.date_local, d.summary,
               d.need_human_review, d.status
        FROM diagnosis d
        LEFT JOIN review_feedback f ON f.diagnosis_id = d.diagnosis_id
        WHERE f.feedback_id IS NULL AND d.need_human_review = 1
        ORDER BY d.date_local DESC, d.diagnosis_id DESC
        LIMIT :lim
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params={"lim": limit})


def _loss_skus(engine: Engine, day: date, limit: int = 10) -> pd.DataFrame:
    start = day - timedelta(days=6)
    sql = """
        SELECT sku_id, shop_id,
               ROUND(SUM(net_revenue_cny), 2) revenue,
               ROUND(SUM(net_profit_cny), 2) profit,
               CASE WHEN SUM(net_revenue_cny) > 0
                    THEN ROUND(SUM(net_profit_cny) / SUM(net_revenue_cny), 4)
                    ELSE NULL END margin
        FROM mart_sku_daily
        WHERE date_local BETWEEN :start AND :d
        GROUP BY sku_id, shop_id
        HAVING profit < 0
        ORDER BY profit ASC
        LIMIT :lim
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params={"start": start, "d": day, "lim": limit})


def _stock_risks(engine: Engine, day: date) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(DISTINCT sku_id, shop_id) FROM mart_sku_daily"
                    " WHERE date_local = :d"
                    " AND stock_status IN ('STOCKOUT_RISK', 'OUT_OF_STOCK')"
                ),
                {"d": day},
            ).scalar_one()
        )


def latest_day(engine: Engine) -> date:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT MAX(date_local) FROM mart_sku_daily")).scalar_one()
    if value is None:
        raise RuntimeError("mart_sku_daily is empty; run generate/ingest/metrics first")
    return pd.Timestamp(value).date()


def _fmt(value: float, unit: str = "") -> str:
    if unit == "pct":
        return f"{value:.1%}"
    if unit == "cny":
        return f"{value:,.0f}"
    return f"{value:,.0f}"


def build(engine: Engine, on_date: date | None = None) -> DailyReport:
    day = on_date or latest_day(engine)
    kpi = _kpi(engine, day)
    prev_kpi = _kpi(engine, day - timedelta(days=7))
    top = _top_anomalies(engine, day)
    pending = _pending_reviews(engine)
    loss = _loss_skus(engine, day)
    risks = _stock_risks(engine, day)

    def delta(key: str) -> str:
        prev = prev_kpi.get(key, 0.0)
        if not prev:
            return "—"
        return f"{(kpi[key] - prev) / abs(prev):+.1%}"

    lines: list[str] = []
    lines.append(f"# 经营日报 · {day.isoformat()}\n")
    lines.append("## 核心指标（对比上周同日）\n")
    lines.append("| 指标 | 当日 | 环比 |")
    lines.append("| --- | ---: | ---: |")
    lines.append(f"| GMV | {_fmt(kpi['gmv'], 'cny')} 元 | {delta('gmv')} |")
    lines.append(f"| 净收入 | {_fmt(kpi['revenue'], 'cny')} 元 | {delta('revenue')} |")
    lines.append(f"| 净利 | {_fmt(kpi['profit'], 'cny')} 元 | {delta('profit')} |")
    lines.append(f"| 净利率 | {_fmt(kpi['margin'], 'pct')} | {delta('margin')} |")
    lines.append(f"| 订单数 | {_fmt(kpi['orders'])} | {delta('orders')} |")
    lines.append(f"| TACOS | {_fmt(kpi['tacos'], 'pct')} | {delta('tacos')} |")
    lines.append("")

    lines.append("## 今日新增异常（按利润影响排序）\n")
    if top.empty:
        lines.append("_今日没有 P0/P1 异常_")
    else:
        lines.append("| 规则 | 严重度 | SKU | 店铺 | 7日利润影响 |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for row in top.itertuples(index=False):
            lines.append(
                f"| {row.rule_id} | {row.severity} | {row.sku_id} | {row.shop_id} "
                f"| {float(row.impact):,.0f} 元 |"
            )
    lines.append("")

    lines.append("## 待人工复核（AI 诊断）\n")
    if pending.empty:
        lines.append("_没有待审诊断_")
    else:
        for row in pending.head(5).itertuples(index=False):
            lines.append(f"- **{row.sku_id} / {row.shop_id}**（{row.rule_id}）：{row.summary}")
    lines.append("")

    lines.append("## 近 7 天亏损 SKU\n")
    if loss.empty:
        lines.append("_没有亏损 SKU_")
    else:
        lines.append("| SKU | 店铺 | 净收入 | 净利 | 净利率 |")
        lines.append("| --- | --- | ---: | ---: | ---: |")
        for row in loss.itertuples(index=False):
            margin = (
                "—" if pd.isna(row.margin) or row.margin is None else f"{float(row.margin):.1%}"
            )
            lines.append(
                f"| {row.sku_id} | {row.shop_id} | {float(row.revenue):,.0f} "
                f"| {float(row.profit):,.0f} | {margin} |"
            )
    lines.append("")
    lines.append(f"## 库存风险\n\n当前断货风险/缺货 SKU：**{risks}** 个\n")

    return DailyReport(
        on_date=day,
        kpi=kpi,
        prev_kpi=prev_kpi,
        top_anomalies=top,
        pending_reviews=pending,
        loss_skus=loss,
        stock_risks=risks,
        markdown="\n".join(lines),
    )


def write(
    engine: Engine, settings: Settings | None = None, on_date: date | None = None
) -> tuple[Path, Path]:
    settings = settings or get_settings()
    report = build(engine, on_date)
    out_dir = Path(settings.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"daily_{report.on_date.isoformat()}.md"
    xlsx_path = out_dir / f"daily_{report.on_date.isoformat()}.xlsx"
    md_path.write_text(report.markdown, encoding="utf-8")

    with pd.ExcelWriter(xlsx_path, engine="xlsxwriter") as writer:
        pd.DataFrame(
            [
                {"指标": k, "当日": v, "上周同日": report.prev_kpi.get(k, 0.0)}
                for k, v in report.kpi.items()
            ]
        ).to_excel(writer, sheet_name="KPI", index=False)
        report.top_anomalies.to_excel(writer, sheet_name="今日异常", index=False)
        report.pending_reviews.to_excel(writer, sheet_name="待审诊断", index=False)
        report.loss_skus.to_excel(writer, sheet_name="亏损SKU", index=False)
    return md_path, xlsx_path
