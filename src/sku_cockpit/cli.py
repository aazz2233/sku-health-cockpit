"""Command line entry points: generate / ingest / report."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sku_cockpit.config import get_settings
from sku_cockpit.data.generator import generate
from sku_cockpit.data.ingest import ingest_all
from sku_cockpit.data.quality import write_report
from sku_cockpit.db import get_engine, init_schema


def _generate(args: argparse.Namespace) -> Path:
    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.data_seed
    n_skus = args.skus if args.skus is not None else settings.data_skus
    days = args.days if args.days is not None else settings.data_days
    started = time.perf_counter()
    result = generate(seed=seed, n_skus=n_skus, days=days)
    elapsed = time.perf_counter() - started
    print(f"[generate] seed={seed} skus={n_skus} days={days} in {elapsed:.1f}s")
    for meta in result.files:
        print(f"  {meta.file:28} rows={meta.rows:>7,}  dirty={len(meta.dirty)}")
    print(f"[generate] output: {result.output_dir}")
    return result.output_dir


def _ingest(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = get_engine(settings)
    if args.drop:
        init_schema(engine, drop=True)
        print("[ingest] schema dropped and recreated")
    else:
        init_schema(engine)
    started = time.perf_counter()
    report = ingest_all(engine, settings, shops=args.shops)
    elapsed = time.perf_counter() - started
    totals = report.totals
    print(
        f"[ingest] files={totals['files']} loaded={totals['rows_loaded']:,} "
        f"rejected={totals['rows_rejected']:,} skipped={totals['skipped']} in {elapsed:.1f}s"
    )
    for reason, count in report.reject_counts().items():
        print(f"  reject {reason:18} {count:>5,}")
    if args.report:
        out = Path(settings.reports_dir) / "data_quality.md"
        write_report(
            engine,
            report,
            Path(settings.generated_dir) / f"seed{settings.data_seed}" / "manifest.json",
            out,
            settings,
        )
        print(f"[ingest] report: {out}")


def _report(_: argparse.Namespace) -> None:
    settings = get_settings()
    from sku_cockpit.data.ingest import IngestReport

    engine = get_engine(settings)
    out = Path(settings.reports_dir) / "data_quality.md"
    write_report(
        engine,
        IngestReport(),
        Path(settings.generated_dir) / f"seed{settings.data_seed}" / "manifest.json",
        out,
        settings,
    )
    print(f"[report] {out}")


def _metrics(_: argparse.Namespace) -> None:
    from sku_cockpit.metrics import rebuild_marts

    settings = get_settings()
    engine = get_engine(settings)
    init_schema(engine)
    started = time.perf_counter()
    counts = rebuild_marts(engine, settings)
    elapsed = time.perf_counter() - started
    print(f"[metrics] built in {elapsed:.1f}s")
    for name, value in counts.items():
        print(f"  {name:20} {value:>10,}")


def _detect(_: argparse.Namespace) -> None:
    from sku_cockpit.anomaly import run as detect_run
    from sku_cockpit.anomaly import write_eval_report

    settings = get_settings()
    engine = get_engine(settings)
    init_schema(engine)
    started = time.perf_counter()
    _events, eval_df = detect_run(engine, settings)
    elapsed = time.perf_counter() - started
    n = eval_df.attrs.get("event_count", 0)
    print(f"[detect] {n} events in {elapsed:.1f}s")
    print(eval_df.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    out = Path(settings.reports_dir) / "anomaly_eval.md"
    write_eval_report(eval_df, out)
    print(f"[detect] report: {out}")


def _diagnose(args: argparse.Namespace) -> None:
    import json

    from sku_cockpit.diagnosis import run as diagnose_run
    from sku_cockpit.diagnosis import write_report

    settings = get_settings()
    engine = get_engine(settings)
    init_schema(engine)
    started = time.perf_counter()
    results, stats = diagnose_run(engine, settings, limit=args.limit)
    elapsed = time.perf_counter() - started
    print(json.dumps(stats.summary(), indent=2, ensure_ascii=False))
    print(f"[diagnose] {len(results)} cards in {elapsed:.1f}s")
    out = Path(settings.reports_dir) / "diagnosis_eval.md"
    write_report(results, stats, out)
    print(f"[diagnose] report: {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sku_cockpit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate synthetic exports")
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--skus", type=int, default=None)
    gen.add_argument("--days", type=int, default=None)
    gen.set_defaults(func=_generate)

    ing = sub.add_parser("ingest", help="load exports into MySQL")
    ing.add_argument("--drop", action="store_true", help="drop and recreate schema first")
    ing.add_argument("--shops", nargs="*", default=None, help="limit to these shop ids")
    ing.add_argument("--report", action="store_true", help="write the quality report afterwards")
    ing.set_defaults(func=_ingest)

    rep = sub.add_parser("report", help="write the data quality report")
    rep.set_defaults(func=_report)

    met = sub.add_parser("metrics", help="rebuild the mart layer from sql/ templates")
    met.set_defaults(func=_metrics)

    det = sub.add_parser("detect", help="run anomaly rules and evaluate against labels")
    det.set_defaults(func=_detect)

    dia = sub.add_parser("diagnose", help="AI-diagnose top anomaly events")
    dia.add_argument("--limit", type=int, default=200, help="how many events to diagnose")
    dia.set_defaults(func=_diagnose)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
