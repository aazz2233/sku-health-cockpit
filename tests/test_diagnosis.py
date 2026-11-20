"""Unit tests for the AI diagnosis layer (no network, no database)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sku_cockpit.diagnosis import (
    Action,
    Cause,
    CitedMetric,
    DiagnosisOut,
    _extract_json,
    fallback_diagnosis,
    validate_citations,
)

PACK: dict[str, dict[str, object]] = {
    "net_margin_30d": {"label": "净利率", "value": -0.12, "unit": "pct"},
    "tacos_30d": {"label": "TACOS", "value": 0.08, "unit": "pct"},
    "stock_status": {"label": "库存状态", "value": "STOCKOUT_RISK", "unit": "enum"},
}


def _model(**overrides: object) -> DiagnosisOut:
    base: dict[str, object] = {
        "summary": "test",
        "likely_causes": [],
        "recommended_actions": [],
        "need_human_review": True,
        "missing_info": [],
        "cited_metrics": [],
    }
    base.update(overrides)
    return DiagnosisOut.model_validate(base)


def test_extract_json_handles_markdown_fence() -> None:
    raw = '```json\n{"summary": "x"}\n```'
    assert _extract_json(raw) == '{"summary": "x"}'


def test_extract_json_handles_prose_wrapper() -> None:
    raw = '这是结果：{"summary": "x"} 希望有帮助'
    assert _extract_json(raw).startswith("{")
    assert _extract_json(raw).endswith("}")


def test_extract_json_passes_plain_json() -> None:
    assert _extract_json('{"a": 1}') == '{"a": 1}'


def test_citation_with_matching_values_is_clean() -> None:
    out = _model(cited_metrics=[{"metric_id": "net_margin_30d", "value": -0.12}])
    assert validate_citations(out, PACK) == []


def test_citation_tolerates_rounding() -> None:
    out = _model(cited_metrics=[{"metric_id": "net_margin_30d", "value": -0.1204}])
    assert validate_citations(out, PACK) == []


def test_citation_of_unknown_metric_is_rejected() -> None:
    out = _model(cited_metrics=[{"metric_id": "made_up_metric", "value": 1.0}])
    errors = validate_citations(out, PACK)
    assert any("unknown metric_id" in e for e in errors)


def test_citation_of_wrong_value_is_rejected() -> None:
    out = _model(cited_metrics=[{"metric_id": "tacos_30d", "value": 0.5}])
    errors = validate_citations(out, PACK)
    assert any("!=" in e for e in errors)


def test_enum_citation_must_match_exactly() -> None:
    ok = _model(cited_metrics=[{"metric_id": "stock_status", "value": "STOCKOUT_RISK"}])
    assert validate_citations(ok, PACK) == []
    bad = _model(cited_metrics=[{"metric_id": "stock_status", "value": "HEALTHY"}])
    assert validate_citations(bad, PACK)


def test_numeric_metric_cited_as_string_is_rejected() -> None:
    out = _model(cited_metrics=[{"metric_id": "tacos_30d", "value": "0.08"}])
    errors = validate_citations(out, PACK)
    assert any("expected number" in e for e in errors)


def test_supporting_metric_ids_are_checked() -> None:
    out = _model(
        likely_causes=[
            Cause(cause="x", supporting_metric_ids=["net_margin_30d", "ghost"], confidence=0.5)
        ]
    )
    errors = validate_citations(out, PACK)
    assert any("ghost" in e for e in errors)


def test_confidence_out_of_range_fails_validation() -> None:
    with pytest.raises(ValidationError):
        Cause(cause="x", supporting_metric_ids=[], confidence=1.5)


def test_fallback_produces_valid_schema_and_cites_known_metrics() -> None:
    event = {"rule_id": "R1", "severity": "P0"}
    out = fallback_diagnosis(event, PACK, "boom")
    assert isinstance(out, DiagnosisOut)
    assert out.need_human_review is True
    assert "boom" in (out.missing_info[0] if out.missing_info else "")
    for cited in out.cited_metrics:
        assert cited.metric_id in PACK
    assert validate_citations(out, PACK) == []


def test_action_defaults_are_usable() -> None:
    a = Action(action="查看明细")
    assert a.priority == 1
    assert isinstance(a.risk, str)


def test_cited_metric_accepts_string_value() -> None:
    c = CitedMetric(metric_id="stock_status", value="LOW")
    assert c.value == "LOW"
