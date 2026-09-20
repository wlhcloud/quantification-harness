from __future__ import annotations

import functools
import json
import re
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .meta import MetaStore
from .upstream import TushareClient, UpstreamError
from .util import now_iso, open_wal, sql_value

CODE_RE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$")
DATE_RE = re.compile(r"^\d{8}$")
IDS = ["fundamentals.indicator", "fundamentals.income", "fundamentals.balance_sheet", "fundamentals.cashflow"]
TABLES = ["financial_indicator", "income_statement", "balance_sheet", "cashflow_statement"]


def _serialized_write(method):
    """串行化会写共享连接的实例方法。

    修复：sync_all_stocks 的 3 个 worker 共享 self.db，各自 BEGIN/commit/rollback ——
    并发 BEGIN 会抛 "cannot start a transaction within a transaction"，并且一个线程的
    rollback 可能把另一个线程刚写入的数据一起回滚掉（静默丢数据）。
    这里以写锁把整段操作串行（网络取数本来就在 sync 内部用线程池并行，锁只影响落库阶段）。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return method(self, *args, **kwargs)
    return wrapper


class FinanceSync:
    def __init__(self, db_path: Path, client: TushareClient, meta: MetaStore, on_progress=None):
        self.client = client
        self.meta = meta
        self.on_progress = on_progress
        self.db = open_wal(db_path)
        self._write_lock = threading.RLock()
        self._create_schema()
        self.bulk: dict[str, Any] = {"status": "idle", "totalSteps": 0, "completedSteps": 0,
                                     "current": None, "startedAt": None, "finishedAt": None, "error": None}

    def _create_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS financial_indicator(code TEXT,end_date TEXT,ann_date TEXT,roe REAL,roa REAL,gross_margin REAL,net_margin REAL,debt_to_assets REAL,current_ratio REAL,netprofit_yoy REAL,revenue_yoy REAL,q_sales_yoy REAL,ocf_yoy REAL,raw_json TEXT,source TEXT,PRIMARY KEY(code,end_date,ann_date));
            CREATE TABLE IF NOT EXISTS income_statement(code TEXT,end_date TEXT,ann_date TEXT,report_type TEXT,revenue REAL,total_revenue REAL,net_income REAL,net_income_parent REAL,operate_profit REAL,raw_json TEXT,source TEXT,PRIMARY KEY(code,end_date,ann_date,report_type));
            CREATE TABLE IF NOT EXISTS balance_sheet(code TEXT,end_date TEXT,ann_date TEXT,report_type TEXT,total_assets REAL,total_liab REAL,total_equity REAL,money_cap REAL,accounts_receiv REAL,inventories REAL,goodwill REAL,raw_json TEXT,source TEXT,PRIMARY KEY(code,end_date,ann_date,report_type));
            CREATE TABLE IF NOT EXISTS cashflow_statement(code TEXT,end_date TEXT,ann_date TEXT,report_type TEXT,net_profit REAL,operating_cashflow REAL,investing_cashflow REAL,financing_cashflow REAL,free_cashflow REAL,cash_end REAL,raw_json TEXT,source TEXT,PRIMARY KEY(code,end_date,ann_date,report_type));
        """)
        self.db.commit()

    def _count(self, table: str) -> int:
        return self.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    def _latest(self, table: str) -> str | None:
        return self.db.execute(f"SELECT MAX(ann_date) latest FROM {table}").fetchone()["latest"]

    def register_datasets(self) -> None:
        latest = max((self._latest(t) or "" for t in TABLES), default=None) or None
        for i, identifier in enumerate(IDS):
            row_count = self._count(TABLES[i])
            self.meta.register_dataset(identifier, "dsh-quant-sync", "quarterly", latest, row_count, "ready" if row_count else "missing")

    @_serialized_write
    def sync(self, code: str, start_date: str, refresh_catalog: bool = True) -> dict[str, Any]:
        if not CODE_RE.match(code) or not DATE_RE.match(start_date):
            raise UpstreamError("code or start_date is invalid")
        with ThreadPoolExecutor(max_workers=4) as pool:
            indicator, income, balance, cashflow = pool.map(
                lambda spec: self.client.call(spec[0], {"ts_code": code, "start_date": start_date}, dataset=spec[1]),
                [("fina_indicator", "fundamentals.indicator"), ("income", None), ("balancesheet", None), ("cashflow", None)],
            )
        self.db.execute("BEGIN")
        try:
            for k, r in enumerate(indicator.rows):
                raw = indicator.raw_rows[k] if k < len(indicator.raw_rows) else r
                self.db.execute("INSERT OR REPLACE INTO financial_indicator VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("roe")),
                     sql_value(r.get("roa")), sql_value(r.get("gross_margin")), sql_value(r.get("net_margin")), sql_value(r.get("debt_to_assets")),
                     sql_value(r.get("current_ratio")), sql_value(r.get("netprofit_yoy")), sql_value(r.get("revenue_yoy")),
                     sql_value(r.get("q_sales_yoy")), sql_value(r.get("ocf_yoy")), json.dumps(raw, ensure_ascii=False), indicator.source))
            for r in income.rows:
                self.db.execute("INSERT OR REPLACE INTO income_statement VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                     sql_value(r.get("revenue")), sql_value(r.get("total_revenue")), sql_value(r.get("n_income")), sql_value(r.get("n_income_attr_p")),
                     sql_value(r.get("operate_profit")), json.dumps(r, ensure_ascii=False), income.source))
            for r in balance.rows:
                self.db.execute("INSERT OR REPLACE INTO balance_sheet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                     sql_value(r.get("total_assets")), sql_value(r.get("total_liab")), sql_value(r.get("total_hldr_eqy_inc_min_int")),
                     sql_value(r.get("money_cap")), sql_value(r.get("accounts_receiv")), sql_value(r.get("inventories")),
                     sql_value(r.get("goodwill")), json.dumps(r, ensure_ascii=False), balance.source))
            for r in cashflow.rows:
                self.db.execute("INSERT OR REPLACE INTO cashflow_statement VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                     sql_value(r.get("net_profit")), sql_value(r.get("n_cashflow_act")), sql_value(r.get("n_cashflow_inv_act")),
                     sql_value(r.get("n_cash_flows_fnc_act")), sql_value(r.get("free_cashflow")), sql_value(r.get("c_cash_equ_end_period")),
                     json.dumps(r, ensure_ascii=False), cashflow.source))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        latest = max((str(r.get("ann_date") or "") for r in indicator.rows), default=None) or None
        counts = None
        if refresh_catalog:
            counts = {"indicator": self._count("financial_indicator"), "income": self._count("income_statement"),
                      "balanceSheet": self._count("balance_sheet"), "cashflow": self._count("cashflow_statement")}
            for i, identifier in enumerate(IDS):
                key = ["indicator", "income", "balanceSheet", "cashflow"][i]
                self.meta.register_dataset(identifier, "dsh-quant-sync", "quarterly", latest, counts[key], "ready" if counts[key] else "missing")
        return {"sources": {"indicator": indicator.source, "income": income.source, "balanceSheet": balance.source, "cashflow": cashflow.source}, "counts": counts}

    def sync_market(self, periods: list[str]) -> None:
        self.bulk.update({"status": "running", "totalSteps": len(periods) * 4, "completedSteps": 0,
                          "current": None, "startedAt": now_iso(), "finishedAt": None, "error": None})
        try:
            for period in periods:
                for api, dataset in [("fina_indicator", "fundamentals.indicator"), ("income", None), ("balancesheet", None), ("cashflow", None)]:
                    self.bulk["current"] = f"{api}:{period}"
                    result = self.client.call(api, {"period": period, "limit": "10000"}, dataset=dataset)
                    self._write_result(api, result.rows, result.raw_rows, result.source)
                    self.bulk["completedSteps"] += 1
                    if self.on_progress:
                        self.on_progress()
            self.register_datasets()
            self.bulk.update({"status": "complete", "current": None, "finishedAt": now_iso()})
        except Exception as error:  # noqa: BLE001
            self.bulk.update({"status": "error", "error": str(error), "finishedAt": now_iso()})

    @_serialized_write
    def _write_result(self, api: str, rows: list[dict], raw_rows: list[dict], source: str) -> None:
        self.db.execute("BEGIN")
        try:
            if api == "fina_indicator":
                for k, r in enumerate(rows):
                    raw = raw_rows[k] if k < len(raw_rows) else r
                    self.db.execute("INSERT OR REPLACE INTO financial_indicator VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("roe")),
                         sql_value(r.get("roa")), sql_value(r.get("gross_margin")), sql_value(r.get("net_margin")), sql_value(r.get("debt_to_assets")),
                         sql_value(r.get("current_ratio")), sql_value(r.get("netprofit_yoy")), sql_value(r.get("revenue_yoy")),
                         sql_value(r.get("q_sales_yoy")), sql_value(r.get("ocf_yoy")), json.dumps(raw, ensure_ascii=False), source))
            elif api == "income":
                for r in rows:
                    self.db.execute("INSERT OR REPLACE INTO income_statement VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("revenue")), sql_value(r.get("total_revenue")), sql_value(r.get("n_income")), sql_value(r.get("n_income_attr_p")),
                         sql_value(r.get("operate_profit")), json.dumps(r, ensure_ascii=False), source))
            elif api == "balancesheet":
                for r in rows:
                    self.db.execute("INSERT OR REPLACE INTO balance_sheet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("total_assets")), sql_value(r.get("total_liab")), sql_value(r.get("total_hldr_eqy_inc_min_int")),
                         sql_value(r.get("money_cap")), sql_value(r.get("accounts_receiv")), sql_value(r.get("inventories")),
                         sql_value(r.get("goodwill")), json.dumps(r, ensure_ascii=False), source))
            else:
                for r in rows:
                    self.db.execute("INSERT OR REPLACE INTO cashflow_statement VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("ts_code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("net_profit")), sql_value(r.get("n_cashflow_act")), sql_value(r.get("n_cashflow_inv_act")),
                         sql_value(r.get("n_cash_flows_fnc_act")), sql_value(r.get("free_cashflow")), sql_value(r.get("c_cash_equ_end_period")),
                         json.dumps(r, ensure_ascii=False), source))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def sync_all_stocks(self, start_date: str) -> None:
        master = self.client.call("stock_basic", {"list_status": "L", "limit": "10000"}, dataset="market.security_master").rows
        codes = [str(r.get("code") or "") for r in master if CODE_RE.match(str(r.get("code") or ""))]
        self.bulk.update({"totalSteps": len(codes), "completedSteps": 0, "current": None, "startedAt": now_iso()})
        cursor = 0
        lock = __import__("threading").Lock()

        def worker() -> None:
            nonlocal cursor
            while True:
                with lock:
                    if cursor >= len(codes):
                        return
                    code = codes[cursor]
                    cursor += 1
                self.bulk["current"] = code
                # 共享连接上的读也要避开其它 worker 的事务（否则会读到未提交数据）
                with self._write_lock:
                    existing = self.db.execute("SELECT COUNT(*) c FROM financial_indicator WHERE code=?", (code,)).fetchone()["c"]
                    if existing < 4:
                        self.sync(code, start_date, refresh_catalog=False)
                self.bulk["completedSteps"] += 1
                if self.on_progress:
                    self.on_progress()

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: worker(), range(min(3, len(codes)))))
        self.register_datasets()
        self.bulk.update({"status": "complete", "current": None, "finishedAt": now_iso()})

    def last_quarters(self, n: int) -> list[str]:
        now = datetime.now()
        q = (now.month - 1) // 3 - 1
        y = now.year
        if q < 0:
            q += 4
            y -= 1
        out: list[str] = []
        for i in range(n):
            qq = q - i
            yy = y
            while qq < 0:
                qq += 4
                yy -= 1
            month = (qq + 1) * 3
            day = "31" if qq in (0, 3) else "30"
            out.append(f"{yy}{month:02d}{day}")
        return out

    def sync_incremental(self, quarters: int) -> None:
        try:
            self.sync_market(self.last_quarters(quarters))
        except Exception as error:  # noqa: BLE001
            self.bulk["error"] = str(error)
        self.sync_all_stocks("20240101")
