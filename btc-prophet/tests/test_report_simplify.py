import io

from openpyxl import load_workbook

from prophet.app import create_app
from prophet.v2reporting import LEGACY_OPTIONAL, write_workbook
from test_v2 import setup


def test_full_audit_omits_only_empty_legacy_sheets_and_lists_them(tmp_path, monkeypatch):
    monkeypatch.delenv("PROPHET_REPORT_KEEP_EMPTY", raising=False)
    s, _, _ = setup(tmp_path)
    create_app(s.path, collect=False, auth_required=False).state.paper.recover_startup(1000)
    out = io.BytesIO()
    write_workbook(s.path, out)
    wb = load_workbook(io.BytesIO(out.getvalue()), read_only=True)
    names = set(wb.sheetnames)
    assert not names & LEGACY_OPTIONAL
    assert {"Bug Log", "Unified Trades", "Exit Shadow Comparison", "Model Selection Evidence",
            "Omitted Empty Sheets"} <= names
    listed = {r[0] for r in list(wb["Omitted Empty Sheets"].iter_rows(values_only=True))[1:]}
    assert listed and listed <= LEGACY_OPTIONAL


def test_keep_empty_setting_restores_every_sheet(tmp_path, monkeypatch):
    monkeypatch.setenv("PROPHET_REPORT_KEEP_EMPTY", "1")
    s, _, _ = setup(tmp_path)
    create_app(s.path, collect=False, auth_required=False)
    out = io.BytesIO()
    write_workbook(s.path, out)
    names = set(load_workbook(io.BytesIO(out.getvalue()), read_only=True).sheetnames)
    assert "Omitted Empty Sheets" not in names and "Roulette Pick History" in names
