"""P4 AI diagnosis: explain an anomaly with metrics it is *not allowed* to invent.

Three guards against hallucination:
  1. metric whitelist - the model only sees pre-computed numbers with ids
  2. citation check  - every cited id must exist and match the whitelist value
  3. degradation     - after retries, fall back to a template, never to garbage
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, text

from sku_cockpit.config import Settings, get_settings
from sku_cockpit.schema import diagnosis

SYSTEM_PROMPT = """你是跨境电商（Amazon）的运营分析师。
你会收到一个异常事件，以及一组已经算好的指标（每个指标有 metric_id、value、unit）。

铁律：
1. 只能引用给定指标里的 metric_id，不得编造、不得自己计算新数字。
2. 每个结论必须挂 supporting_metric_ids，指向具体指标。
3. 无法从给定指标判断原因时，把 need_human_review 设为 true，并在 missing_info 里写缺什么。
4. 只输出 JSON，不要任何额外文字。
5. 精简：最多 3 条原因、3 条建议，每条不超过 60 字，不要复述指标数值。

输出 JSON 结构：
{
  "summary": "一句话结论",
  "likely_causes": [{"cause": "...", "supporting_metric_ids": ["..."], "confidence": 0.0-1.0}],
  "recommended_actions": [{"action": "...", "priority": 1, "expected_effect": "...", "risk": "..."}],
  "need_human_review": true/false,
  "missing_info": ["..."],
  "cited_metrics": [{"metric_id": "...", "value": 123.4, "period": "30d"}]
}"""


class CitedMetric(BaseModel):
    metric_id: str
    # enums (e.g. stock_status) are legitimate citations, so allow strings too
    value: float | str
    period: str = "30d"


class Cause(BaseModel):
    cause: str
    supporting_metric_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Action(BaseModel):
    action: str
    priority: int = 1
    expected_effect: str = ""
    risk: str = ""


class DiagnosisOut(BaseModel):
    summary: str
    likely_causes: list[Cause] = Field(default_factory=list)
    recommended_actions: list[Action] = Field(default_factory=list)
    need_human_review: bool = True
    missing_info: list[str] = Field(default_factory=list)
    cited_metrics: list[CitedMetric] = Field(default_factory=list)


MetricPack = dict[str, dict[str, Any]]


@dataclass
class DiagnosisStats:
    attempted: int = 0
    llm_ok: int = 0
    fallback: int = 0
    citation_errors: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latencies: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        total = max(1, self.attempted)
        return {
            "attempted": self.attempted,
            "llm_ok": self.llm_ok,
            "fallback": self.fallback,
            "citation_error_rate": round(self.citation_errors / total, 4),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_latency_ms": int(sum(self.latencies) / len(self.latencies))
            if self.latencies
            else 0,
        }


def _add(pack: MetricPack, key: str, label: str, value: Any, unit: str) -> None:
    if value is None:
        return
    if isinstance(value, float):
        value = round(value, 4)
    pack[key] = {"label": label, "value": value, "unit": unit}


def select_events(engine: Engine, limit: int = 200) -> list[dict[str, Any]]:
    """Top events by 7-day profit impact, one card per SKU-shop-day."""
    sql = """
        SELECT event_id, rule_id, sku_id, shop_id, date_local, severity, evidence, impact
        FROM (
            SELECT t.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sku_id, shop_id, date_local
                       ORDER BY impact DESC, event_id
                   ) AS rn
            FROM (
                SELECT e.event_id, e.rule_id, e.sku_id, e.shop_id, e.date_local,
                       e.severity, e.evidence,
                       ABS(COALESCE((
                           SELECT SUM(m.net_profit_cny) FROM mart_sku_daily m
                           WHERE m.sku_id = e.sku_id AND m.shop_id = e.shop_id
                             AND m.date_local BETWEEN DATE_SUB(e.date_local, INTERVAL 6 DAY)
                                                  AND e.date_local
                       ), 0)) AS impact
                FROM anomaly_event e
                WHERE e.severity IN ('P0', 'P1') AND e.date_local IS NOT NULL
            ) t
        ) u
        WHERE rn = 1
        ORDER BY impact DESC, event_id
        LIMIT :lim
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def build_metric_pack(engine: Engine, event: dict[str, Any]) -> MetricPack:
    sku = event["sku_id"]
    shop = event["shop_id"]
    day = event["date_local"]
    d0 = day - timedelta(days=29)
    prev1 = d0 - timedelta(days=1)
    prev0 = prev1 - timedelta(days=29)

    pack: MetricPack = {}
    with engine.connect() as conn:
        cur = (
            conn.execute(
                text(
                    "SELECT SUM(net_revenue_cny) rev, SUM(net_profit_cny) profit, SUM(gmv_cny) gmv,"
                    " SUM(cogs_cny) cogs, SUM(referral_fee_cny) referral, SUM(fba_fee_cny) fba,"
                    " SUM(storage_fee_cny) storage, SUM(ad_spend_cny) ads, SUM(refund_cny) refund,"
                    " SUM(return_loss_cny) return_loss, SUM(units) units, SUM(orders) orders"
                    " FROM mart_sku_daily WHERE sku_id=:sku AND shop_id=:shop"
                    " AND date_local BETWEEN :d0 AND :d1"
                ),
                {"sku": sku, "shop": shop, "d0": d0, "d1": day},
            )
            .mappings()
            .one()
        )
        prev = (
            conn.execute(
                text(
                    "SELECT SUM(net_revenue_cny) rev, SUM(net_profit_cny) profit"
                    " FROM mart_sku_daily WHERE sku_id=:sku AND shop_id=:shop"
                    " AND date_local BETWEEN :d0 AND :d1"
                ),
                {"sku": sku, "shop": shop, "d0": prev0, "d1": prev1},
            )
            .mappings()
            .one()
        )
        stock = (
            conn.execute(
                text(
                    "SELECT available_qty, days_of_cover, stock_status, reorder_point"
                    " FROM mart_sku_daily WHERE sku_id=:sku AND shop_id=:shop"
                    " AND date_local <= :d1 ORDER BY date_local DESC LIMIT 1"
                ),
                {"sku": sku, "shop": shop, "d1": day},
            )
            .mappings()
            .first()
        )
        cat = (
            conn.execute(
                text(
                    "SELECT s.category, SUM(m.net_profit_cny) profit, SUM(m.net_revenue_cny) rev,"
                    " SUM(m.refund_cny) refund, SUM(m.gmv_cny) gmv"
                    " FROM mart_sku_daily m JOIN dim_sku s ON s.sku_id = m.sku_id"
                    " WHERE m.sku_id = :sku AND m.shop_id = :shop AND m.date_local BETWEEN :d0 AND :d1"
                    " GROUP BY s.category"
                ),
                {"sku": sku, "shop": shop, "d0": d0, "d1": day},
            )
            .mappings()
            .first()
        )
        category = str(cat["category"]) if cat else None

    rev = float(cur["rev"] or 0)
    gmv = float(cur["gmv"] or 0)
    profit = float(cur["profit"] or 0)
    cogs = float(cur["cogs"] or 0)
    ads = float(cur["ads"] or 0)
    refund = float(cur["refund"] or 0)
    return_loss = float(cur["return_loss"] or 0)

    _add(pack, "rev_30d", "近30天净收入", rev, "CNY")
    _add(pack, "gmv_30d", "近30天GMV", gmv, "CNY")
    _add(pack, "net_profit_30d", "近30天净利", profit, "CNY")
    _add(pack, "net_margin_30d", "近30天净利率", profit / rev if rev else None, "pct")
    _add(
        pack,
        "gross_margin_30d",
        "近30天毛利率",
        (rev - cogs - return_loss) / rev if rev else None,
        "pct",
    )
    _add(pack, "cogs_30d", "近30天商品成本", cogs, "CNY")
    _add(pack, "referral_fee_30d", "近30天佣金", float(cur["referral"] or 0), "CNY")
    _add(pack, "fba_fee_30d", "近30天FBA配送费", float(cur["fba"] or 0), "CNY")
    _add(pack, "storage_fee_30d", "近30天仓储费", float(cur["storage"] or 0), "CNY")
    _add(pack, "ad_spend_30d", "近30天广告费", ads, "CNY")
    _add(pack, "tacos_30d", "近30天TACOS", ads / rev if rev else None, "pct")
    _add(pack, "refund_30d", "近30天退款金额", refund, "CNY")
    _add(pack, "refund_rate_30d", "近30天退款率", refund / gmv if gmv else None, "pct")
    _add(pack, "return_loss_30d", "近30天退货货损", return_loss, "CNY")
    _add(pack, "units_30d", "近30天销量", int(cur["units"] or 0), "units")
    _add(pack, "orders_30d", "近30天订单数", int(cur["orders"] or 0), "orders")

    prev_rev = float(prev["rev"] or 0) if prev else 0.0
    prev_profit = float(prev["profit"] or 0) if prev else 0.0
    if prev_rev:
        _add(pack, "net_margin_prev_30d", "前30天净利率", prev_profit / prev_rev, "pct")

    if stock:
        _add(pack, "available_qty", "当前可售库存", stock["available_qty"], "units")
        _add(pack, "days_of_cover", "可售天数", stock["days_of_cover"], "days")
        _add(pack, "reorder_point", "补货点", stock["reorder_point"], "units")
        _add(pack, "stock_status", "库存状态", str(stock["stock_status"]), "enum")

    if cat and category:
        cat_rev = float(cat["rev"] or 0)
        cat_gmv = float(cat["gmv"] or 0)
        _add(pack, "category", "类目", category, "enum")
        _add(
            pack,
            "category_net_margin_30d",
            "同店同类目近30天净利率",
            (float(cat["profit"] or 0) / cat_rev) if cat_rev else None,
            "pct",
        )
        _add(
            pack,
            "category_refund_rate_30d",
            "同店同类目近30天退款率",
            (float(cat["refund"] or 0) / cat_gmv) if cat_gmv else None,
            "pct",
        )
    return pack


def build_user_prompt(event: dict[str, Any], pack: MetricPack) -> str:
    payload = {
        "event": {
            "rule_id": event["rule_id"],
            "severity": event["severity"],
            "sku_id": event["sku_id"],
            "shop_id": event["shop_id"],
            "date": str(event["date_local"]),
            "trigger_evidence": event["evidence"],
        },
        "metrics": {
            k: {"label": v["label"], "value": v["value"], "unit": v["unit"]}
            for k, v in pack.items()
        },
    }
    return "请诊断下面这个异常事件。只能引用 metrics 里的 metric_id。\n\n" + json.dumps(
        payload, ensure_ascii=False, default=str, indent=2
    )


def validate_citations(out: DiagnosisOut, pack: MetricPack) -> list[str]:
    errors: list[str] = []
    for cited in out.cited_metrics:
        if cited.metric_id not in pack:
            errors.append(f"unknown metric_id: {cited.metric_id}")
            continue
        actual = pack[cited.metric_id]["value"]
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            if isinstance(cited.value, str):
                errors.append(f"{cited.metric_id}: expected number, got string")
                continue
            tolerance = max(0.01, abs(float(actual)) * 0.01)
            if abs(float(cited.value) - float(actual)) > tolerance:
                errors.append(f"{cited.metric_id}: cited {cited.value} != actual {actual}")
        elif isinstance(actual, str) and str(cited.value) != actual:
            errors.append(f"{cited.metric_id}: cited {cited.value!r} != actual {actual!r}")
    for cause in out.likely_causes:
        for mid in cause.supporting_metric_ids:
            if mid not in pack:
                errors.append(f"unknown supporting metric_id: {mid}")
    return errors


def fallback_diagnosis(event: dict[str, Any], pack: MetricPack, reason: str) -> DiagnosisOut:
    """Template explanation built only from whitelisted numbers."""
    margin = pack.get("net_margin_30d", {}).get("value")
    rev = pack.get("rev_30d", {}).get("value", 0)
    ads = pack.get("ad_spend_30d", {}).get("value", 0)
    refund = pack.get("refund_30d", {}).get("value", 0)
    parts = [f"规则 {event['rule_id']} 触发"]
    if margin is not None:
        parts.append(f"近30天净利率 {margin:.1%}")
    if rev:
        parts.append(f"净收入 {rev:,.0f} 元")
    if rev and ads:
        parts.append(f"广告费占比 {ads / rev:.1%}")
    if rev and refund:
        parts.append(f"退款占比 {refund / rev:.1%}")
    return DiagnosisOut(
        summary="；".join(parts) + "。" + reason,
        likely_causes=[
            Cause(
                cause="需要人工复核：自动归因未通过校验，先按指标异常程度排序",
                supporting_metric_ids=[k for k in ("net_margin_30d", "tacos_30d") if k in pack],
                confidence=0.3,
            )
        ],
        recommended_actions=[
            Action(
                action="人工查看该 SKU 近 30 天的成本、广告与退款明细",
                priority=1,
                expected_effect="确认是否为定价、广告或退货问题",
                risk="未定位前不建议直接调价或加投广告",
            )
        ],
        need_human_review=True,
        missing_info=["自动归因失败，原因如下：" + reason],
        cited_metrics=[
            CitedMetric(metric_id=k, value=float(pack[k]["value"]))
            for k in ("net_margin_30d", "tacos_30d")
            if k in pack and isinstance(pack[k]["value"], (int, float))
        ],
    )


def _extract_json(text: str) -> str:
    """Models often wrap JSON in markdown fences even with response_format=json."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        return t[start : end + 1]
    return t


def _call_llm(settings: Settings, messages: list[dict[str, str]]) -> tuple[str, int, int]:
    """Call the model over the OpenAI-compatible protocol.

    litellm's built-in DeepSeek adapter only knows deepseek-chat/deepseek-reasoner;
    this account exposes deepseek-v4-pro, so we go through the generic openai/
    route with an explicit api_base instead of relying on provider mapping.
    """
    from litellm import completion

    model = settings.llm_model or ""
    kwargs: dict[str, Any] = {
        "temperature": 0.2,
        "timeout": settings.llm_timeout_seconds,
        "max_tokens": settings.llm_max_tokens,
        "response_format": {"type": "json_object"},
    }
    if settings.llm_disable_thinking:
        # without this a reasoning model spends the whole max_tokens on
        # reasoning_content and returns an empty message
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    if settings.llm_api_base:
        model = f"openai/{model}"
        kwargs["api_base"] = settings.llm_api_base
        if settings.deepseek_api_key:
            kwargs["api_key"] = settings.deepseek_api_key
    elif "/" not in model:
        model = f"{settings.llm_provider}/{model}"

    resp = completion(model=model, messages=messages, **kwargs)
    content = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    return (
        content,
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def diagnose_one(
    engine: Engine,
    event: dict[str, Any],
    settings: Settings,
    stats: DiagnosisStats,
) -> dict[str, Any]:
    pack = build_metric_pack(engine, event)
    stats.attempted += 1
    status = "llm"
    tokens_in = tokens_out = 0
    started = time.perf_counter()

    out: DiagnosisOut | None = None
    last_reason = ""
    if settings.llm_provider != "mock" and settings.llm_model:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(event, pack)},
        ]
        for _attempt in range(settings.llm_max_retries + 1):
            try:
                raw, tin, tout = _call_llm(settings, messages)
                tokens_in += tin
                tokens_out += tout
                candidate = DiagnosisOut.model_validate(json.loads(_extract_json(raw)))
                errors = validate_citations(candidate, pack)
                if not errors:
                    out = candidate
                    break
                last_reason = "; ".join(errors[:3])
                stats.citation_errors += 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": "引用校验失败："
                        + last_reason
                        + "。只能使用给定 metric_id，请重新输出 JSON。",
                    },
                ]
            except (ValidationError, json.JSONDecodeError, Exception) as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
    else:
        last_reason = "LLM disabled (mock provider)"

    if out is None:
        status = "fallback"
        stats.fallback += 1
        out = fallback_diagnosis(event, pack, last_reason[:200])
    else:
        stats.llm_ok += 1

    latency = int((time.perf_counter() - started) * 1000)
    stats.latencies.append(latency)
    stats.tokens_in += tokens_in
    stats.tokens_out += tokens_out

    record = {
        "event_id": int(event["event_id"]),
        "rule_id": event["rule_id"],
        "sku_id": event["sku_id"],
        "shop_id": event["shop_id"],
        "date_local": event["date_local"],
        "model": f"{settings.llm_provider}/{settings.llm_model or 'mock'}",
        "summary": out.summary[:1000],
        "likely_causes": [c.model_dump() for c in out.likely_causes],
        "recommended_actions": [a.model_dump() for a in out.recommended_actions],
        "cited_metrics": [c.model_dump() for c in out.cited_metrics],
        "need_human_review": out.need_human_review,
        "status": status,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency,
    }
    with engine.begin() as conn:
        conn.execute(diagnosis.insert(), record)
    return record


def run(
    engine: Engine, settings: Settings | None = None, limit: int = 200
) -> tuple[list[dict], DiagnosisStats]:
    settings = settings or get_settings()
    events = select_events(engine, limit=limit)
    stats = DiagnosisStats()
    results: list[dict[str, Any]] = []
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM diagnosis"))
    for event in events:
        results.append(diagnose_one(engine, event, settings, stats))
    return results, stats


def write_report(results: list[dict[str, Any]], stats: DiagnosisStats, out_path) -> None:
    s = stats.summary()
    lines = ["# AI 归因评估报告\n"]
    lines.append("| 指标 | 值 |")
    lines.append("| --- | ---: |")
    for k, v in s.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## 样例（前 5 条）\n")
    for r in results[:5]:
        lines.append(f"### {r['sku_id']} / {r['shop_id']} / {r['rule_id']} / {r['date_local']}")
        lines.append(f"- 状态：`{r['status']}`，需人审：{r['need_human_review']}")
        lines.append(f"- 结论：{r['summary']}")
        for c in r["likely_causes"][:2]:
            lines.append(f"- 原因：{c['cause']}（置信度 {c['confidence']}）")
        for a in r["recommended_actions"][:2]:
            lines.append(f"- 建议：{a['action']}（优先级 {a['priority']}）")
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
