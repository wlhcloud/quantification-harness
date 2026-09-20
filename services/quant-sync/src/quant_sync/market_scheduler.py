"""Transparent market-session scheduler for homepage index data."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .tool_sync import ToolSync
from .util import now_iso

CHINA = ZoneInfo("Asia/Shanghai")


class MarketScheduler:
    def __init__(self, tools: ToolSync, market_path: Path, config: Callable[[], dict[str, Any]]):
        self.tools = tools
        self.market_path = market_path
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_slot: str | None = None
        self._catchup_day: str | None = None
        self._calendar_requested_day: str | None = None
        self.state: dict[str, Any] = {
            "status": "stopped", "marketState": "unknown", "lastCheckAt": None,
            "lastTriggeredAt": None, "nextCheckAt": None, "lastError": None,
            "lastSlot": None, "catchupDay": None,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.state["status"] = "running"
        self._thread = threading.Thread(target=self._loop, name="market-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.state["status"] = "stopped"

    def snapshot(self) -> dict[str, Any]:
        cfg = self.config().get("marketScheduler", {})
        return {**self.state, "enabled": bool(cfg.get("enabled", True)),
                "intervalSeconds": int(cfg.get("intervalSeconds", 60)),
                "catchupEnabled": bool(cfg.get("catchupEnabled", True)),
                "timezone": "Asia/Shanghai", "sessions": ["09:30-11:30", "13:00-15:00"]}

    def _is_open_day(self, day: str) -> tuple[bool | None, str]:
        try:
            db = sqlite3.connect(f"file:{self.market_path}?mode=ro", uri=True, timeout=2)
            row = db.execute("SELECT is_open FROM trade_calendar WHERE calendar_date=? AND exchange='SSE'", (day,)).fetchone()
            db.close()
            if row is not None:
                return bool(row[0]), "calendar"
        except sqlite3.Error:
            pass
        return None, "missing"

    @staticmethod
    def _in_session(now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        return 570 <= minute <= 690 or 780 <= minute <= 900

    def _day_complete(self, day: str) -> bool:
        iso_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        try:
            db = sqlite3.connect(f"file:{self.market_path}?mode=ro", uri=True, timeout=2)
            rows = db.execute("""SELECT code,COUNT(DISTINCT substr(trade_time,1,16)) points
                FROM index_minute_bars WHERE substr(trade_time,1,10)=? AND upper(freq)='1MIN'
                GROUP BY code HAVING points>=200""", (iso_day,)).fetchall()
            db.close()
            return len(rows) >= 5
        except sqlite3.Error:
            return False

    def _safe_start(self, tool_id: str, params: dict[str, Any]) -> bool:
        if self.tools.states[tool_id]["status"] in {"running", "starting"}:
            return False
        self.tools.start(tool_id, params, trigger_source="market-scheduler")
        return True

    def _tick(self) -> None:
        now = datetime.now(CHINA)
        day = now.strftime("%Y%m%d")
        open_day, calendar_source = self._is_open_day(day)
        if open_day is None:
            if self._calendar_requested_day != day:
                self._calendar_requested_day = day
                if self._safe_start("trade_cal", {"start_date": day, "end_date": day}):
                    self.state.update({"lastCheckAt": now_iso(), "marketState": "calendar-updating",
                                       "calendarSource": "sync-requested", "lastTriggeredAt": now_iso(), "lastError": None})
                    return
            if self.tools.states["trade_cal"]["status"] in {"running", "starting"}:
                self.state.update({"lastCheckAt": now_iso(), "marketState": "calendar-updating", "calendarSource": "sync-running"})
                return
            open_day, calendar_source = now.weekday() < 5, "weekday-fallback-after-sync-error"
            if self.tools.states["trade_cal"]["status"] == "error":
                self.state["lastError"] = self.tools.states["trade_cal"].get("error")
        in_session = open_day and self._in_session(now)
        self.state.update({"lastCheckAt": now_iso(), "marketState": "trading" if in_session else ("closed" if open_day else "non-trading-day"),
                           "calendarSource": calendar_source, "lastError": None})
        cfg = self.config().get("marketScheduler", {})
        if not cfg.get("enabled", True) or not open_day:
            return
        if cfg.get("catchupEnabled", True) and self._catchup_day != day:
            if self._day_complete(day):
                self._catchup_day = day
                self.state["catchupDay"] = day
            else:
                if self._safe_start("idx_mins", {"trade_date": day, "freq": "1min"}):
                    self._catchup_day = day
                    self.state.update({"catchupDay": day, "lastTriggeredAt": now_iso()})
                    return
        if not in_session:
            return
        interval = max(30, min(300, int(cfg.get("intervalSeconds", 60))))
        slot = f"{day}-{int(now.timestamp()) // interval}"
        if slot == self._last_slot:
            return
        started = self._safe_start("rt_idx_k", {})
        started = self._safe_start("rt_idx_min", {"freq": "1MIN"}) or started
        if started:
            self._last_slot = slot
            self.state.update({"lastSlot": slot, "lastTriggeredAt": now_iso()})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as error:  # scheduler stays alive and exposes the failure
                self.state["lastError"] = str(error)
            cfg = self.config().get("marketScheduler", {})
            interval = max(30, min(300, int(cfg.get("intervalSeconds", 60))))
            self.state["nextCheckAt"] = (datetime.now(CHINA) + timedelta(seconds=min(5, interval))).isoformat()
            self._stop.wait(min(5, interval))
