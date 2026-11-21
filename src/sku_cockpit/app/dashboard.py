"""Streamlit dashboard: overview, SKU health, drill-down and review queue.

Run with:
    uv run streamlit run src/sku_cockpit/app/dashboard.py
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import text

from sku_cockpit.config import get_settings
from sku_cockpit.db import get_engine

st.set_page_config(page_title="SKU Health Cockpit", page_icon="📦", layout="wide")


# --------------------------------------------------------------------- data
@st.cache_resource
def _engine():
    return get_engine(get_settings())


@st.cache_data(ttl=120)
def load_daily() -> pd.DataFrame:
    with _engine().connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT date_local, sku_id, shop_id, units, orders, gmv_cny, net_revenue_cny,"
                " net_profit_cny, net_margin_pct, gross_margin_pct, tacos_pct, ad_spend_cny,"
                " refund_cny, cogs_cny, referral_fee_cny, fba_fee_cny, storage_fee_cny,"
                " return_loss_cny, available_qty, days_of_cover, stock_status"
                " FROM mart_sku_daily"
            ),
            conn,
        )
    df["date_local"] = pd.to_datetime(df["date_local"])
    return df


@st.cache_data(ttl=120)
def load_meta() -> pd.DataFrame:
    with _engine().connect() as conn:
        return pd.read_sql(
            text("SELECT sku_id, title, brand, category, unit_cost_cny FROM dim_sku"), conn
        )


@st.cache_data(ttl=120)
def load_events() -> pd.DataFrame:
    with _engine().connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT event_id, rule_id, sku_id, shop_id, date_local, severity FROM anomaly_event"
            ),
            conn,
        )
    df["date_local"] = pd.to_datetime(df["date_local"], errors="coerce")
    return df


@st.cache_data(ttl=60)
def load_diagnoses() -> pd.DataFrame:
    with _engine().connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT d.diagnosis_id, d.sku_id, d.shop_id, d.rule_id, d.date_local,
                       d.summary, d.likely_causes, d.recommended_actions, d.cited_metrics,
                       d.need_human_review, d.status, f.feedback_id, f.decision
                FROM diagnosis d
                LEFT JOIN review_feedback f ON f.diagnosis_id = d.diagnosis_id
                ORDER BY d.date_local DESC, d.diagnosis_id DESC
                """
            ),
            conn,
        )
    return df


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


STATUS_LABELS = {
    "OUT_OF_STOCK": "缺货",
    "STOCKOUT_RISK": "断货风险",
    "LOW": "库存偏低",
    "HEALTHY": "健康",
    "OVERSTOCK": "超储",
}


def status_label(value: Any) -> str:
    return STATUS_LABELS.get(str(value), str(value))


def _latest_date(daily: pd.DataFrame) -> date:
    return daily["date_local"].max().date()


# ------------------------------------------------------------------- pages
def page_overview() -> None:
    st.title("📦 SKU 经营总览")
    daily = load_daily()
    events = load_events()
    last = _latest_date(daily)

    today = daily[daily["date_local"] == pd.Timestamp(last)]
    prev = daily[daily["date_local"] == pd.Timestamp(last - pd.Timedelta(days=7))]
    recent_events = events[events["date_local"] >= pd.Timestamp(last - pd.Timedelta(days=6))]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当日 GMV", f"{today['gmv_cny'].sum():,.0f} 元")
    profit = today["net_profit_cny"].sum()
    revenue = today["net_revenue_cny"].sum()
    c2.metric("当日净利", f"{profit:,.0f} 元")
    c3.metric("净利率", f"{profit / revenue:.1%}" if revenue else "—")
    c4.metric("近 7 天 P0/P1 异常", f"{len(recent_events):,} 条")

    st.caption(f"数据截止 {last}｜对比上周同日：GMV {_delta(today, prev, 'gmv_cny')}")

    trend = (
        daily.groupby("date_local", as_index=False)
        .agg(gmv=("gmv_cny", "sum"), profit=("net_profit_cny", "sum"))
        .rename(columns={"gmv": "净收入", "profit": "净利"})
        .sort_values("date_local")
    )
    fig = px.line(
        trend, x="date_local", y=["净收入", "净利"], labels={"value": "CNY", "date_local": ""}
    )
    fig.update_layout(height=320, legend_title_text="", margin={"l": 0, "r": 0, "t": 10, "b": 0})
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns([1, 1])
    with left:
        st.subheader("异常规则分布（近 7 天）")
        by_rule = recent_events.groupby("rule_id").size().reset_index(name="事件数")
        st.plotly_chart(
            px.bar(by_rule, x="rule_id", y="事件数", text="事件数").update_layout(
                height=300, margin={"l": 0, "r": 0, "t": 10, "b": 0}
            ),
            width="stretch",
        )
    with right:
        st.subheader("店铺表现（近 30 天）")
        start = pd.Timestamp(last - pd.Timedelta(days=29))
        shop = (
            daily[daily["date_local"] >= start]
            .groupby("shop_id", as_index=False)
            .agg(revenue=("net_revenue_cny", "sum"), profit=("net_profit_cny", "sum"))
        )
        shop["net_margin"] = (shop["profit"] / shop["revenue"].replace(0, pd.NA)) * 100
        st.dataframe(
            shop.rename(
                columns={
                    "shop_id": "店铺",
                    "revenue": "净收入",
                    "profit": "净利",
                    "net_margin": "净利率",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "净利率": st.column_config.NumberColumn(format="%.1f%%"),
                "净收入": st.column_config.NumberColumn(format="%.0f"),
                "净利": st.column_config.NumberColumn(format="%.0f"),
            },
        )


def _delta(today: pd.DataFrame, prev: pd.DataFrame, column: str) -> str:
    a, b = today[column].sum(), prev[column].sum()
    if not b:
        return "—"
    return f"{(a - b) / abs(b):+.1%}"


def page_health() -> None:
    st.title("🩺 SKU 健康列表")
    daily = load_daily()
    meta = load_meta()
    last = _latest_date(daily)

    snapshot = daily[daily["date_local"] == pd.Timestamp(last)].merge(
        meta[["sku_id", "category", "title"]], on="sku_id", how="left"
    )
    start = pd.Timestamp(last - pd.Timedelta(days=29))
    window = (
        daily[daily["date_local"] >= start]
        .groupby(["sku_id", "shop_id"], as_index=False)
        .agg(rev_30d=("net_revenue_cny", "sum"), profit_30d=("net_profit_cny", "sum"))
    )
    snapshot = snapshot.merge(window, on=["sku_id", "shop_id"], how="left")
    snapshot["margin_30d"] = snapshot["profit_30d"] / snapshot["rev_30d"].replace(0, pd.NA) * 100

    c1, c2, c3 = st.columns([2, 2, 2])
    shops = c1.multiselect(
        "店铺", sorted(snapshot["shop_id"].unique()), default=sorted(snapshot["shop_id"].unique())
    )
    cats = c2.multiselect("类目", sorted(snapshot["category"].dropna().unique()))
    statuses = c3.multiselect("库存状态", sorted(snapshot["stock_status"].dropna().unique()))

    view = snapshot[snapshot["shop_id"].isin(shops)]
    if cats:
        view = view[view["category"].isin(cats)]
    if statuses:
        view = view[view["stock_status"].isin(statuses)]

    only_loss = st.checkbox("只看亏损 SKU（近 30 天净利 < 0）", value=False)
    if only_loss:
        view = view[view["profit_30d"] < 0]

    st.caption(f"{len(view):,} 个 SKU-店铺组合（截至 {last}）")
    view = view.assign(stock_status=view["stock_status"].map(status_label))
    st.dataframe(
        view.sort_values("profit_30d")[
            [
                "sku_id",
                "shop_id",
                "category",
                "title",
                "stock_status",
                "available_qty",
                "days_of_cover",
                "rev_30d",
                "profit_30d",
                "margin_30d",
            ]
        ].rename(
            columns={
                "sku_id": "SKU",
                "shop_id": "店铺",
                "category": "类目",
                "title": "标题",
                "stock_status": "库存状态",
                "available_qty": "可售库存",
                "days_of_cover": "可售天数",
                "rev_30d": "30天净收入",
                "profit_30d": "30天净利",
                "margin_30d": "30天净利率",
            }
        ),
        width="stretch",
        hide_index=True,
        height=560,
        column_config={
            "30天净利率": st.column_config.NumberColumn(format="%.1f%%"),
            "30天净利": st.column_config.NumberColumn(format="%.0f"),
        },
    )


def page_drilldown() -> None:
    st.title("🔍 SKU 下钻")
    daily = load_daily()
    meta = load_meta()
    diag = load_diagnoses()

    skus = sorted(daily["sku_id"].unique())
    c1, c2 = st.columns([3, 1])
    sku = c1.selectbox("选择 SKU", skus, index=0)
    shops = sorted(daily[daily["sku_id"] == sku]["shop_id"].unique())
    shop = c2.selectbox("店铺", shops, index=0)

    view = daily[(daily["sku_id"] == sku) & (daily["shop_id"] == shop)].sort_values("date_local")
    title = meta.loc[meta["sku_id"] == sku, "title"]
    st.caption(title.iloc[0] if len(title) else "")

    last30 = view[view["date_local"] >= view["date_local"].max() - pd.Timedelta(days=29)]
    revenue = last30["net_revenue_cny"].sum()
    profit = last30["net_profit_cny"].sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("30 天净收入", f"{revenue:,.0f} 元")
    c2.metric("30 天净利", f"{profit:,.0f} 元")
    c3.metric("30 天净利率", f"{profit / revenue:.1%}" if revenue else "—")
    c4.metric("当前库存状态", status_label(view.iloc[-1]["stock_status"]))

    chart = last30.rename(columns={"net_revenue_cny": "净收入", "net_profit_cny": "净利"})
    fig = px.line(
        chart, x="date_local", y=["净收入", "净利"], labels={"value": "CNY", "date_local": ""}
    )
    fig.update_layout(height=300, legend_title_text="", margin={"l": 0, "r": 0, "t": 10, "b": 0})
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("30 天成本结构")
        cost = pd.DataFrame(
            {
                "科目": ["商品成本", "佣金", "FBA", "仓储", "广告", "退货货损"],
                "金额": [
                    last30["cogs_cny"].sum(),
                    last30["referral_fee_cny"].sum(),
                    last30["fba_fee_cny"].sum(),
                    last30["storage_fee_cny"].sum(),
                    last30["ad_spend_cny"].sum(),
                    last30["return_loss_cny"].sum(),
                ],
            }
        )
        st.plotly_chart(
            px.pie(cost, names="科目", values="金额", hole=0.45).update_layout(
                height=300, margin={"l": 0, "r": 0, "t": 10, "b": 0}
            ),
            width="stretch",
        )
    with right:
        st.subheader("可售库存（近 30 天）")
        stock = px.line(last30, x="date_local", y="available_qty", labels={"date_local": ""})
        stock.update_layout(height=300, margin={"l": 0, "r": 0, "t": 10, "b": 0})
        st.plotly_chart(stock, width="stretch")

    st.subheader("该 SKU 的异常与诊断")
    mine = diag[(diag["sku_id"] == sku) & (diag["shop_id"] == shop)]
    if mine.empty:
        st.info("没有诊断记录")
    for row in mine.head(5).itertuples(index=False):
        with st.expander(f"{row.date_local} · {row.rule_id} · {row.summary[:60]}"):
            st.write(row.summary)
            for cause in _json(row.likely_causes)[:3]:
                st.markdown(f"- **{cause['cause']}**（置信度 {cause['confidence']}）")


def page_review() -> None:
    st.title("✅ 人审队列")
    diag = load_diagnoses()
    pending = diag[(diag["feedback_id"].isna()) & (diag["need_human_review"] == True)]  # noqa: E712

    st.caption(f"待审 {len(pending)} 条｜已处理 {int(diag['feedback_id'].notna().sum())} 条")
    if pending.empty:
        st.success("队列已清空")
        return

    for row in pending.head(20).itertuples(index=False):
        with st.container(border=True):
            st.markdown(f"**{row.sku_id} · {row.shop_id} · {row.rule_id} · {row.date_local}**")
            st.write(row.summary)
            causes = _json(row.likely_causes)[:3]
            actions = _json(row.recommended_actions)[:3]
            cited = _json(row.cited_metrics)
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**可能原因**")
                for cause in causes:
                    st.markdown(f"- {cause['cause']}（{cause['confidence']}）")
            with c2:
                st.markdown("**建议动作**")
                for action in actions:
                    st.markdown(f"- P{action['priority']}：{action['action']}")
            st.caption("引用指标：" + "、".join(c["metric_id"] for c in cited))

            note = st.text_input(
                "备注",
                key=f"note_{row.diagnosis_id}",
                label_visibility="collapsed",
                placeholder="备注（可留空）",
            )
            b1, b2, _b3 = st.columns([1, 1, 4])
            if b1.button("通过", key=f"ok_{row.diagnosis_id}"):
                _record_feedback(int(row.diagnosis_id), "approve", note)
            if b2.button("驳回", key=f"no_{row.diagnosis_id}"):
                _record_feedback(int(row.diagnosis_id), "reject", note)


def _record_feedback(diagnosis_id: int, decision: str, note: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO review_feedback (diagnosis_id, decision, note, reviewer)"
                " VALUES (:d, :dec, :note, 'dashboard')"
            ),
            {"d": diagnosis_id, "dec": decision, "note": note or None},
        )
    st.cache_data.clear()
    st.toast(f"已记录：{decision}")
    st.rerun()


def _sidebar() -> None:
    with st.sidebar:
        st.markdown("### SKU Health Cockpit")
        st.caption("多店铺 SKU 利润 / 库存 / 异常诊断")
        try:
            daily = load_daily()
            st.metric("数据截止", str(_latest_date(daily)))
            st.caption(f"{daily['sku_id'].nunique()} 个 SKU · {daily['shop_id'].nunique()} 个店铺")
        except Exception:  # pragma: no cover - empty database
            st.warning("数据库为空，请先运行 generate / ingest / metrics")


def main() -> None:
    _sidebar()
    nav = st.navigation(
        [
            st.Page(page_overview, title="总览", icon="📊", default=True),
            st.Page(page_health, title="SKU 健康", icon="🩺", url_path="health"),
            st.Page(page_drilldown, title="SKU 下钻", icon="🔍", url_path="drilldown"),
            st.Page(page_review, title="人审队列", icon="✅", url_path="review"),
        ]
    )
    nav.run()


main()
