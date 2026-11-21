# sku-health-cockpit — 常用命令
# Windows 上没有 make 时，用 scripts/dev.ps1 里的等价命令。
# CI（Linux）走这里，本地 Windows 也建议装 make 或直接用 uv run。

PY := .venv/Scripts/python.exe
UV := uv
export PYTHONPATH := src

.DEFAULT_GOAL := help

.PHONY: help sync fmt lint type test test-cov check ci clean

help:  ## 显示可用命令
	@echo "make sync     安装/更新依赖（需联网，国内建议先设 UV_DEFAULT_INDEX）"
	@echo "make up       一键：生成数据(按需) → 入库 → 指标 → 检测 → 日报（Windows 用 scripts/up.ps1）"
	@echo "make dashboard 启动看板 http://localhost:8501"
	@echo "make report   生成日报（Markdown + Excel）"
	@echo "make fmt      格式化（ruff format）"
	@echo "make lint     静态检查（ruff check）"
	@echo "make type     类型检查（mypy）"
	@echo "make test     跑测试"
	@echo "make ci       本地跑一遍 CI 会跑的全部检查"
	@echo "make clean    清理缓存与临时产物（不删数据与 .venv）"

sync:
	$(UV) sync

up:
	$(UV) run python -m sku_cockpit generate
	$(UV) run python -m sku_cockpit ingest --drop --report
	$(UV) run python -m sku_cockpit metrics
	$(UV) run python -m sku_cockpit detect
	$(UV) run python -m sku_cockpit report-daily

dashboard:
	$(UV) run streamlit run src/sku_cockpit/app/dashboard.py

report:
	$(UV) run python -m sku_cockpit report-daily

fmt:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

type:
	$(UV) run mypy

test:
	$(UV) run pytest

test-cov:
	$(UV) run pytest --cov --cov-report=term-missing

check: lint type test

ci: check

clean:
	$(UV) run python -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache','.mypy_cache','.ruff_cache','htmlcov']]"
