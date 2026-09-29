"""控制台 JSON 接口。

接口只做参数校验收敛与转发，业务判断全部留在组件与顺序执行器里：状态来自
``Platform`` 的状态快照，趋势走趋势缓冲，硫化／开模／调度／停运走四条工艺
顺序。控制台因此不会绕开联锁，也不会自己拼一条工艺链。
"""

from __future__ import annotations

from typing import Callable

from ..app import CycleInputs, Platform
from ..errors import RubberCureError
from ..mold.summary import summarize_molds
from ..press.summary import summarize_fleet

MAX_TREND_WINDOW_S = 86_400.0
MAX_AUDIT_LIMIT = 500

UNAVAILABLE_STATUS = "暂时不可用"


def unavailable(reason: object) -> dict:
    """标记一块读不到的后台数据，其余数据块不受影响。"""

    return {"unavailable": True, "status": UNAVAILABLE_STATUS, "reason": str(reason)}


def safe_block(label: str, producer: Callable[[], object]) -> object:
    """单独取一块数据；读不到时返回不可用标记而不是把整页带崩。"""

    try:
        return producer()
    except Exception as error:  # 控制台是只读视图，任何一块后台数据失败都应降级
        return unavailable(f"{label}读取失败: {error}")


class ConsoleAPI:
    """把 HTTP 请求转成平台动作。"""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def status(self) -> dict:
        """逐块汇总状态快照；单块读不到只影响它自己，其余照常返回。"""

        platform = self.platform
        return {
            "platform": safe_block("平台名", lambda: platform.config.name),
            "project_id": safe_block("项目编号", lambda: platform.config.project_id),
            "unit": safe_block("车间命名空间", lambda: platform.namespaces.unit),
            "cycles": safe_block("周期计数", lambda: platform.cycles),
            "now": safe_block("当前时钟", lambda: round(platform.clock.now(), 3)),
            "utc": safe_block("UTC 时间", lambda: platform.clock.utc_iso()),
            "molds": safe_block("模具台账", lambda: summarize_molds(platform.registry)),
            "sensor_maps": safe_block("传感器映射", lambda: platform.maps.snapshot()),
            "curves": safe_block("曲线代际", lambda: platform.curves.snapshot()),
            "latch": safe_block("完成闩锁", lambda: platform.latch.snapshot()),
            "fleet": safe_block("机群概览", lambda: summarize_fleet(platform.fleet)),
            "scheduler": safe_block("调度器", lambda: platform.scheduler.snapshot()),
            "presses": safe_block("硫化机快照", lambda: platform.fleet.snapshot()),
            "trend": safe_block("趋势快照", lambda: platform.trend.snapshot()),
            "namespaces_by_area": safe_block(
                "命名空间分区", lambda: platform.namespaces_by_area()
            ),
            "audit": safe_block("审计摘要", lambda: platform.audit.summary()),
            "endpoints": safe_block("接口清单", lambda: platform.endpoints()),
            "topology": safe_block("链路拓扑", lambda: platform.topology_summary()),
            "namespaces": safe_block("命名空间", lambda: platform.namespaces.describe()),
        }

    def trend(self, series: str | None = None, window_s: float = 600.0) -> dict:
        """返回趋势窗口：采样点、均值与最新值。"""

        window = max(1.0, min(MAX_TREND_WINDOW_S, float(window_s)))
        names = [series] if series else self.platform.trend.series_names()
        known = self.platform.trend.series_names()
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ValueError(f"未知的趋势分量: {', '.join(unknown)}")
        end = self.platform.clock.now()
        start = end - window
        result: dict[str, dict] = {}
        for name in names:
            result[name] = safe_block(  # type: ignore[assignment]
                f"趋势分量 {name}", lambda name=name: self._trend_series(name, start, end)
            )
        return {"window_s": window, "start": round(start, 3), "end": round(end, 3), "series": result}

    def _trend_series(self, name: str, start: float, end: float) -> dict:
        trend = self.platform.trend
        filled = trend.fill_gaps(name)
        samples = trend.window(name, start, end)
        latest = trend.latest(name)
        return {
            "filled_points": filled,
            "samples": [sample.as_dict() for sample in samples],
            "average": round(trend.average(name, start, end), 4),
            "latest": latest.as_dict() if latest else None,
        }

    def alarms(self) -> dict:
        def read_alarms() -> dict:
            alarms = self.platform.audit.alarms()
            return {"count": len(alarms), "alarms": alarms}

        result = safe_block("告警", read_alarms)
        if isinstance(result, dict) and "unavailable" not in result:
            return result
        # 保持成功时的外壳形状，前端按同一个字段名渲染失败标记
        return {"count": None, "alarms": result}

    def audit(self, limit: int = 100) -> dict:
        bounded = max(1, min(MAX_AUDIT_LIMIT, int(limit)))

        def read_audit() -> dict:
            return {
                "entries": self.platform.audit.entries(bounded),
                "recent": self.platform.audit.tail(bounded // 4 + 1),
            }

        result = safe_block("审计记录", read_audit)
        if isinstance(result, dict) and "unavailable" not in result:
            return result
        return {"entries": result, "recent": result}

    def journal(self, segment: int | None = None, limit: int = 200) -> dict:
        """读落盘日志；给段号时只读该段。"""

        def read_journal() -> dict:
            if segment is None:
                return {"segment": None, "entries": self.platform.audit.replay_from_journal(limit)}
            return {"segment": segment, "entries": self.platform.audit.replay_segment(segment)}

        result = safe_block("落盘日志", read_journal)
        if isinstance(result, dict) and "unavailable" not in result:
            return result
        return {"segment": segment, "entries": result}

    def readiness(self) -> dict:
        """投产前体检：把机群与模具的关键前置条件摊开给控制台。"""

        platform = self.platform
        return {
            "idle_presses": safe_block(
                "空闲机群", lambda: [unit.press_id for unit in platform.fleet.idle_presses()]
            ),
            "loaded_molds": safe_block(
                "在机模具",
                lambda: {
                    unit.press_id: unit.mold_id for unit in platform.fleet.all() if unit.mold_id
                },
            ),
            "molds": safe_block("模具台账", lambda: summarize_count(platform)),
            "latched": safe_block("完成闩锁", lambda: platform.latch.set_molds()),
            "queued": safe_block("调度队列", lambda: platform.scheduler.queue.entries()),
            "curves": safe_block("曲线代际", lambda: sorted(platform.curves.snapshot())),
            "steam_target_kpa": safe_block(
                "蒸汽目标压力", lambda: platform.config.steam.target_pressure_kpa
            ),
            "temp_setpoint_c": safe_block(
                "模温设定", lambda: platform.config.temp.setpoint_c
            ),
        }

    def cure(self, payload: dict) -> dict:
        """在一台硫化机上跑硫化顺序。"""

        mold_id = str(payload.get("mold_id", "mold-01"))
        try:
            return {
                "ok": True,
                "report": self.platform.run_cure(
                    mold_id,
                    product_code=payload.get("product_code"),
                    cavity_count=int(payload.get("cavity_count", 4)),
                    thickness_mm=payload.get("thickness_mm"),
                    press_id=str(payload.get("press_id", "press-01")),
                ),
            }
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def open_mold(self, payload: dict) -> dict:
        try:
            return {
                "ok": True,
                "report": self.platform.run_open(str(payload.get("press_id", "press-01"))),
            }
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def dispatch(self, payload: dict) -> dict:
        try:
            return {
                "ok": True,
                "report": self.platform.run_dispatch(str(payload.get("press_id", "press-01"))),
            }
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def stop(self, payload: dict) -> dict:
        try:
            return {
                "ok": True,
                "report": self.platform.run_stop(str(payload.get("press_id", "press-01"))),
            }
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def step(self, payload: dict) -> dict:
        """推进一个机群控制周期。"""

        inputs = CycleInputs(
            inlet_c=float(payload.get("inlet_c", 26.0)),
            steam_target_kpa=float(
                payload.get("steam_target_kpa", self.platform.config.steam.target_pressure_kpa)
            ),
            dt=float(payload.get("dt", 1.0)),
        )
        try:
            return {"ok": True, "cycle": self.platform.cycle(inputs)}
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def enqueue(self, payload: dict) -> dict:
        mold_id = str(payload.get("mold_id", "mold-01"))
        try:
            return {
                "ok": True,
                "queued": self.platform.enqueue_mold(
                    mold_id,
                    thickness_mm=payload.get("thickness_mm"),
                    priority=int(payload.get("priority", 50)),
                ),
            }
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}

    def remap(self, payload: dict) -> dict:
        """重新登记传感器映射。"""

        mold_id = str(payload.get("mold_id", "mold-01"))
        raw = payload.get("sensors") or {}
        try:
            sensors = {int(cavity): str(sensor) for cavity, sensor in raw.items()}
            return {"ok": True, "mapping": self.platform.remap_sensor(mold_id, sensors)}
        except (RubberCureError, ValueError) as error:
            if isinstance(error, RubberCureError):
                return {"ok": False, "error": self.platform.note_error(error)}
            return {"ok": False, "error": {"type": "ValueError", "message": str(error)}}

    def retire(self, payload: dict) -> dict:
        """把一台模具标记为报废。"""

        mold_id = str(payload.get("mold_id", "mold-01"))
        reason = str(payload.get("reason", "控制台触发的检修报废"))
        try:
            return {"ok": True, "mold": self.platform.retire_mold(mold_id, reason)}
        except RubberCureError as error:
            return {"ok": False, "error": self.platform.note_error(error)}


def summarize_count(platform: Platform) -> dict:
    """统计台账里的模具数量，供体检接口使用。"""

    records = platform.registry.describe()
    return {
        "total": len(records),
        "active": sum(1 for record in records if record["status"] == "active"),
    }
