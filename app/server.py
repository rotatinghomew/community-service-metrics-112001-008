"""本地 Web 应用：提供分析 API 与静态界面。

仅使用 Python 3.11 标准库。默认读取 ``fixtures/``，状态保存在 ``.state/state.json``。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .analytics import analyse, diff_plan
from .config import DEFAULT_SERVICE_MINUTES, Scenario, ScenarioError
from .data import DataError, load_dataset
from .weeks import WeekFormatError, parse_week, shift_week

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 512 * 1024
ADJUSTMENT_FIELDS = {"id", "kind", "grid", "week", "category", "count_delta", "to_grid", "note"}


class StateError(ValueError):
    pass


class Store:
    """进程内加锁的 JSON 状态存储；写入使用临时文件替换，避免半截文件。"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"adjustments": [], "scenarios": [], "confirmed_plan": None}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(payload, dict):
            for key in ("adjustments", "scenarios"):
                if isinstance(payload.get(key), list):
                    self.data[key] = payload[key]
            if isinstance(payload.get("confirmed_plan"), (dict, type(None))):
                self.data["confirmed_plan"] = payload["confirmed_plan"]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def add_adjustment(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = _validate_adjustment(payload)
        with self.lock:
            if any(item["id"] == record["id"] for item in self.data["adjustments"]):
                raise StateError(f"修订 id {record['id']} 已存在。")
            self.data["adjustments"].append(record)
            self._save()
        return record

    def delete_adjustment(self, adjustment_id: str) -> None:
        with self.lock:
            before = len(self.data["adjustments"])
            self.data["adjustments"] = [
                item for item in self.data["adjustments"] if item["id"] != adjustment_id
            ]
            if len(self.data["adjustments"]) == before:
                raise StateError(f"找不到修订 {adjustment_id}。")
            self._save()

    def add_scenario(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = Scenario.from_dict(payload.get("scenario"))
        label = str(payload.get("label") or scenario.label or "").strip()
        if not label:
            raise StateError("保存情景需要名称。")
        record = {
            "id": "SC-" + uuid.uuid4().hex[:8],
            "label": label[:60],
            "scenario": scenario.to_dict(),
        }
        with self.lock:
            if len(self.data["scenarios"]) >= 8:
                raise StateError("最多保存 8 个情景；请先删除不用的情景。")
            self.data["scenarios"].append(record)
            self._save()
        return record

    def delete_scenario(self, scenario_id: str) -> None:
        with self.lock:
            before = len(self.data["scenarios"])
            self.data["scenarios"] = [
                item for item in self.data["scenarios"] if item["id"] != scenario_id
            ]
            if len(self.data["scenarios"]) == before:
                raise StateError(f"找不到情景 {scenario_id}。")
            self._save()

    def confirm_plan(self, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        if not label:
            raise StateError("确认班表需要填写名称或版本说明。")
        assignments = result["schedule"]["assignments"]
        if not assignments:
            raise StateError("当前建议没有任何可确认的班次分配，未写入确认班表。")
        record = {
            "id": "CF-" + uuid.uuid4().hex[:8],
            "label": label[:80],
            "confirmed_at": payload.get("confirmed_at") or "",
            "scenario": result["scenario"],
            "start_week": result["meta"]["start_week"],
            "weeks": result["meta"]["weeks"],
            "input_fingerprint": result["meta"]["input_fingerprint"],
            "assignments": assignments,
            "summary": result["summary"],
            "warnings": result["meta"]["warnings"][:20],
        }
        with self.lock:
            self.data["confirmed_plan"] = record
            self._save()
        return record

    def clear_plan(self) -> None:
        with self.lock:
            self.data["confirmed_plan"] = None
            self._save()


def _validate_adjustment(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StateError("修订记录应为对象。")
    kind = payload.get("kind")
    if kind not in {"cancel", "transfer", "supplement"}:
        raise StateError("修订类型只能是 cancel、transfer 或 supplement。")
    grid = str(payload.get("grid") or "").strip()
    category = str(payload.get("category") or "").strip()
    week = str(payload.get("week") or "").strip()
    if not grid or not category:
        raise StateError("修订需要网格与事项类别。")
    try:
        parse_week(week)
    except WeekFormatError as exc:
        raise StateError(str(exc)) from exc
    delta = payload.get("count_delta")
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        raise StateError("count_delta 应为非零整数。")
    if kind in {"cancel", "transfer"} and delta > 0:
        raise StateError("撤销/转派的 count_delta 应为负数（表示减少量）。")
    if kind == "supplement" and delta < 0:
        raise StateError("补录的 count_delta 应为正数。")
    to_grid = payload.get("to_grid")
    if kind == "transfer":
        to_grid = str(to_grid or "").strip()
        if not to_grid or to_grid == grid:
            raise StateError("转派需要不同于原网格的 to_grid。")
    else:
        to_grid = None
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise StateError("note 应为文本。")
    return {
        "id": str(payload.get("id") or "ADJ-" + uuid.uuid4().hex[:8])[:40],
        "kind": kind,
        "grid": grid,
        "week": week,
        "category": category,
        "count_delta": delta,
        "to_grid": to_grid,
        "note": (note or "").strip()[:200],
        "origin": "runtime",
    }


def _catalogue(dataset, store: Store) -> dict[str, Any]:
    state = store.snapshot()
    weeks = sorted({a.week for a in dataset.arrivals}, key=parse_week)
    latest = dataset.latest_arrival_week()
    options = []
    if latest:
        options.append(latest)
        options.extend(shift_week(latest, step) for step in range(1, 5))
    return {
        "grids": dataset.grids,
        "categories": dataset.categories,
        "skills": sorted({s.skill for s in dataset.skills}),
        "people": sorted({s.person for s in dataset.shifts}),
        "arrival_weeks": weeks,
        "shift_weeks": sorted({s.week for s in dataset.shifts}, key=parse_week),
        "start_week_default": shift_week(latest, 1) if latest else (dataset.first_shift_week() or ""),
        "start_week_options": options,
        "calendar": [
            {"week": c.week, "grid": c.grid, "event": c.event, "factor": c.factor}
            for c in dataset.calendar
        ],
        "arrivals": [
            {
                "id": a.id,
                "grid": a.grid,
                "week": a.week,
                "category": a.category,
                "count": a.count,
                "entered_late": a.entered_late,
            }
            for a in dataset.arrivals
        ],
        "shifts": [
            {
                "id": s.id,
                "person": s.person,
                "week": s.week,
                "grid": s.grid,
                "start": s.start,
                "end": s.end,
                "break_minutes": s.break_minutes,
            }
            for s in dataset.shifts
        ],
        "skills": [
            {
                "person": s.person,
                "grid": s.grid,
                "skill": s.skill,
                "valid_from": s.valid_from,
                "valid_to": s.valid_to,
            }
            for s in dataset.skills
        ],
        "default_service_minutes": DEFAULT_SERVICE_MINUTES,
        "data_warnings": dataset.warnings,
        "state": {
            "adjustments": state["adjustments"],
            "scenarios": state["scenarios"],
            "confirmed_plan": _plan_summary(state["confirmed_plan"]),
        },
    }


def _plan_summary(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan:
        return None
    return {
        "id": plan.get("id"),
        "label": plan.get("label"),
        "confirmed_at": plan.get("confirmed_at"),
        "start_week": plan.get("start_week"),
        "weeks": plan.get("weeks"),
        "input_fingerprint": plan.get("input_fingerprint"),
        "assignments": plan.get("assignments", []),
        "summary": plan.get("summary"),
        "warnings": plan.get("warnings", []),
    }


class Handler(SimpleHTTPRequestHandler):
    dataset_root: Path = ROOT / "fixtures"
    store: Store

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # 保持终端安静，只记录错误
        pass

    # ---------- helpers ----------

    def _dataset(self):
        try:
            return load_dataset(self.dataset_root)
        except (DataError, OSError) as exc:
            self._json({"error": str(exc), "kind": "data"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return None

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST, kind: str = "validation") -> None:
        self._json({"error": message, "kind": kind}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise StateError("请求体过大。")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError(f"请求体不是有效 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise StateError("请求体应为对象。")
        return payload

    # ---------- routing ----------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            dataset = self._dataset()
            if dataset is None:
                return
            self._json(_catalogue(dataset, self.store))
            return
        if path == "/api/health":
            self._json({"ok": True})
            return
        if path.startswith("/api/"):
            self._error("未知接口。", HTTPStatus.NOT_FOUND, "not_found")
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._body()
            if path == "/api/analyse":
                self._handle_analyse(payload)
            elif path == "/api/adjustments":
                record = self.store.add_adjustment(payload)
                self._json({"ok": True, "adjustment": record})
            elif path == "/api/scenarios":
                record = self.store.add_scenario(payload)
                self._json({"ok": True, "scenario": record})
            elif path == "/api/plan/confirm":
                self._handle_confirm(payload)
            else:
                self._error("未知接口。", HTTPStatus.NOT_FOUND, "not_found")
        except (StateError, ScenarioError, DataError) as exc:
            self._error(str(exc))
        except Exception as exc:  # pragma: no cover - 最后防线
            self._error(f"服务器内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR, "internal")

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/adjustments/"):
                self.store.delete_adjustment(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/scenarios/"):
                self.store.delete_scenario(path.rsplit("/", 1)[-1])
            elif path == "/api/plan/confirmed":
                self.store.clear_plan()
            else:
                self._error("未知接口。", HTTPStatus.NOT_FOUND, "not_found")
                return
            self._json({"ok": True})
        except StateError as exc:
            self._error(str(exc), HTTPStatus.NOT_FOUND)

    # ---------- handlers ----------

    def _handle_analyse(self, payload: dict[str, Any]) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        scenario = Scenario.from_dict(payload.get("scenario"))
        state = self.store.snapshot()
        runtime_adjustments = state["adjustments"]
        if payload.get("ignore_runtime_adjustments") is True:
            runtime_adjustments = []
        try:
            result = analyse(dataset, scenario, runtime_adjustments)
        except DataError as exc:
            self._error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY, "data")
            return
        confirmed = state["confirmed_plan"]
        result["plan_diff"] = (
            diff_plan(confirmed["assignments"], result["schedule"]["assignments"])
            if confirmed
            else None
        )
        result["confirmed_plan"] = _plan_summary(confirmed)
        result["state_adjustments"] = runtime_adjustments
        self._json(result)

    def _handle_confirm(self, payload: dict[str, Any]) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        scenario = Scenario.from_dict(payload.get("scenario"))
        state = self.store.snapshot()
        result = analyse(dataset, scenario, state["adjustments"])
        try:
            record = self.store.confirm_plan(payload, result)
        except StateError as exc:
            self._error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        self._json({"ok": True, "confirmed_plan": _plan_summary(record)})


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    data_root: Path | str = ROOT / "fixtures",
    state_path: Path | str | None = None,
) -> ThreadingHTTPServer:
    store = Store(Path(state_path) if state_path else ROOT / ".state" / "state.json")
    handler = type(
        "BoundHandler",
        (Handler,),
        {"dataset_root": Path(data_root), "store": store},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="社区服务负荷分析与排班情景应用")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=str(ROOT / "fixtures"))
    parser.add_argument("--state", default=None)
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.data, args.state)
    print(f"服务已启动：http://{args.host}:{args.port}  （Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
