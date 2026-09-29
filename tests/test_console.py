"""控制台：页面、只读接口与四条工艺顺序的 HTTP 入口。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from rubbercure.app import CycleInputs
from rubbercure.console.api import ConsoleAPI, UNAVAILABLE_STATUS
from rubbercure.console.pages import PAGE_TITLES, render_page
from rubbercure.console.server import ConsoleServer, probe_pages
from rubbercure.telemetry import TrendBuffer


def _request(url: str, method: str = "GET", payload: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8"))
            except ValueError:
                return response.status, body.decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def test_probe_pages_reads_every_page(platform):
    server = ConsoleServer(platform)
    base_url = server.start()
    try:
        results = probe_pages(base_url, platform.config.pages)
        assert all(item["ok"] for item in results.values())
        assert "api/status" in results
    finally:
        server.stop()


def test_http_status_and_order_endpoints(platform):
    server = ConsoleServer(platform)
    base_url = server.start()
    try:
        status, payload = _request(f"{base_url}/api/status")
        assert status == 200
        assert payload["platform"] == "RubberCure"
        assert "fleet" in payload
        assert "topology" in payload
        assert "namespaces" in payload

        status, readiness = _request(f"{base_url}/api/readiness")
        assert status == 200
        assert len(readiness["idle_presses"]) == platform.fleet.size

        status, page = _request(f"{base_url}/")
        assert status == 200
        assert "控制台" in page

        status, page = _request(f"{base_url}/timer")
        assert status == 200
        assert PAGE_TITLES["timer"] in page

        status, result = _request(f"{base_url}/api/cure", "POST", {"mold_id": "http-mold"})
        assert status == 200
        assert result["ok"] is True
        assert result["report"]["violations"] == []

        status, result = _request(f"{base_url}/api/step", "POST", {"dt": 1.0})
        assert status == 200
        assert result["ok"] is True
        assert len(result["cycle"]["presses"]) == platform.fleet.size

        status, body = _request(f"{base_url}/api/does-not-exist")
        assert status == 404
        assert "not-found" in body
    finally:
        server.stop()


def test_console_api_actions_cover_maintenance(platform):
    api = ConsoleAPI(platform)
    assert api.alarms()["count"] == 0
    assert api.audit(limit=5)["entries"] is not None
    assert api.trend(window_s=60.0)["series"] == {}
    assert api.journal()["entries"] is not None

    platform.register_mold("api-mold", cavity_count=2)
    added = api.enqueue({"mold_id": "api-mold", "thickness_mm": 6.0})
    assert added["ok"] is True
    assert platform.scheduler.queue.entries()[0]["mold_id"] == "api-mold"

    remapped = api.remap({"mold_id": "api-mold", "sensors": {"0": "api-t0", "1": "api-t1"}})
    assert remapped["ok"] is True
    assert remapped["mapping"]["generation"] == 2
    assert remapped["mapping"]["sensors"]["0"] == "api-t0"

    retired = platform.retire_mold("api-mold", "控制台报废")
    assert retired["status"] == "retired"
    assert platform.scheduler.queue.entries() == []


def test_render_page_rejects_unknown_page(platform):
    try:
        render_page("nope", platform)
    except KeyError as error:
        assert "nope" in str(error)
    else:  # pragma: no cover - 未抛错即失败
        raise AssertionError("未知页面必须报错")


def test_status_isolates_a_failing_block(platform):
    api = ConsoleAPI(platform)

    def broken_summary():
        raise RuntimeError("落盘审计目录不可读")

    platform.audit.summary = broken_summary  # type: ignore[method-assign]

    payload = api.status()
    assert payload["audit"]["unavailable"] is True
    assert payload["audit"]["status"] == UNAVAILABLE_STATUS
    assert "落盘审计目录不可读" in payload["audit"]["reason"]
    # 其余数据块照常返回
    assert payload["platform"] == "RubberCure"
    assert payload["fleet"]["size"] == platform.fleet.size
    assert "missing" in payload["topology"]


def test_status_endpoint_stays_200_when_one_backend_block_fails(platform):
    server = ConsoleServer(platform)
    base_url = server.start()
    try:
        platform.fleet.snapshot = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("机群快照盘损坏")
        )
        status, payload = _request(f"{base_url}/api/status")
        assert status == 200
        assert payload["presses"]["status"] == UNAVAILABLE_STATUS
        assert "机群快照盘损坏" in payload["presses"]["reason"]
        assert payload["platform"] == "RubberCure"
        assert payload["scheduler"]["phase"] is not None
    finally:
        server.stop()


def test_trend_isolates_each_series(platform):
    api = ConsoleAPI(platform)
    platform.cycle(CycleInputs(inlet_c=26.0, steam_target_kpa=600.0, dt=1.0))
    assert set(api.trend()["series"]) == {"fleet_load", "mold_temp", "steam_pressure"}

    original_window = TrendBuffer.window

    def broken_window(self, name, start, end):
        if name == "steam_pressure":
            raise RuntimeError("蒸汽压力缓冲损坏")
        return original_window(self, name, start, end)

    TrendBuffer.window = broken_window  # type: ignore[method-assign]
    try:
        payload = api.trend()
        assert payload["series"]["steam_pressure"]["status"] == UNAVAILABLE_STATUS
        assert payload["series"]["mold_temp"]["latest"] is not None
        assert payload["series"]["fleet_load"]["latest"] is not None
    finally:
        TrendBuffer.window = original_window  # type: ignore[method-assign]


def test_alarms_returns_marker_instead_of_failing(platform):
    api = ConsoleAPI(platform)

    def broken_alarms():
        raise OSError("告警索引读不回来")

    platform.audit.alarms = broken_alarms  # type: ignore[method-assign]

    payload = api.alarms()
    assert payload["alarms"]["unavailable"] is True
    assert payload["alarms"]["status"] == UNAVAILABLE_STATUS
    assert "告警索引读不回来" in payload["alarms"]["reason"]


def test_unexpected_endpoint_error_is_structured_not_crash(platform):
    server = ConsoleServer(platform)
    base_url = server.start()
    try:
        platform.audit.alarms = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("突发故障")
        )
        status, payload = _request(f"{base_url}/api/alarms")
        assert status == 200
        assert payload["alarms"]["status"] == UNAVAILABLE_STATUS
    finally:
        server.stop()


def test_rendered_page_handles_each_block_independently(platform):
    html = render_page("press", platform)
    assert "暂时不可用" in html
    assert "fetchBlock('/api/status'" in html
    assert "fetchBlock('/api/alarms'" in html
    assert "fetchBlock('/api/trend" in html


def test_readiness_isolates_each_precondition(platform):
    api = ConsoleAPI(platform)

    def broken_entries():
        raise RuntimeError("队列段文件丢失")

    platform.scheduler.queue.entries = broken_entries  # type: ignore[method-assign]

    payload = api.readiness()
    assert payload["queued"]["status"] == UNAVAILABLE_STATUS
    assert "队列段文件丢失" in payload["queued"]["reason"]
    # 其余体检项照常返回
    assert len(payload["idle_presses"]) == platform.fleet.size
    assert payload["molds"]["total"] == 0
    assert payload["steam_target_kpa"] == platform.config.steam.target_pressure_kpa
