"""控制台页面。

四个页面分别对应硫化机、蒸汽升温、硫化计时与告警。页面本身只做展示：数据
全部通过 ``/api/status``、``/api/trend``、``/api/alarms`` 拉取，页面里的
按钮走 ``/api/cure``、``/api/open``、``/api/dispatch``、``/api/step``，
不内嵌任何业务逻辑。每块面板独立拉取、独立兜底：某一块后台数据读不出来时
只把那块标成暂时不可用，其余面板照常刷新。
"""

from __future__ import annotations

from ..app import Platform

PAGE_TITLES: dict[str, str] = {
    "press": "硫化机与模具",
    "steam": "蒸汽升温",
    "timer": "硫化计时",
    "alarms": "告警与审计",
}

_STYLE = """
body { font-family: "Microsoft YaHei", sans-serif; margin: 0; background: #14120f; color: #efe6da; }
header { padding: 14px 22px; background: #211d17; display: flex; align-items: center; gap: 18px; }
header h1 { font-size: 17px; margin: 0; font-weight: 600; }
nav a { color: #d9a86b; margin-right: 14px; text-decoration: none; font-size: 14px; }
nav a.active { color: #ffd479; font-weight: 700; }
main { padding: 20px 22px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
section { background: #211d17; border-radius: 8px; padding: 14px 16px; }
section h2 { font-size: 14px; margin: 0 0 10px; color: #d8b98c; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td { padding: 4px 6px; border-bottom: 1px solid #322a21; }
td.k { color: #b09a7d; width: 42%; }
button { background: #8a5a24; color: #fff; border: 0; border-radius: 4px; padding: 7px 14px; margin-right: 8px; cursor: pointer; }
button.warn { background: #8f3629; }
pre { font-size: 12px; background: #0f0d0a; padding: 10px; border-radius: 6px; overflow-x: auto; max-height: 260px; }
"""

_SCRIPT = """
async function fetchJson(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => null);
  if (!response.ok || body === null) {
    throw new Error(url + ' 返回 ' + response.status);
  }
  return body;
}
function showUnavailable(id, error) {
  const el = document.getElementById(id);
  el.style.color = '#e0a34a';
  el.textContent = '⚠ 暂时不可用（' + (error && error.message ? error.message : error) +
    '），其余面板不受影响';
}
function renderStatus(status) {
  const el = document.getElementById('out-status');
  el.style.color = '';
  let text = JSON.stringify(status, null, 2);
  const missing = status.unavailable_sections || [];
  if (missing.length) {
    el.style.color = '#e0a34a';
    text = '⚠ 以下数据块暂时不可用: ' + missing.join(', ') + '\\n\\n' + text;
  }
  el.textContent = text;
}
function renderAlarms(alarms) {
  const el = document.getElementById('out-alarms');
  el.style.color = '';
  if (alarms && alarms.available === false) {
    showUnavailable('out-alarms', alarms.message);
    return;
  }
  el.textContent = alarms.count + ' 条告警\\n' + JSON.stringify(alarms.alarms.slice(-8), null, 2);
}
function renderTrend(trend) {
  const el = document.getElementById('out-trend');
  el.style.color = '';
  if (trend && trend.available === false) {
    showUnavailable('out-trend', trend.message);
    return;
  }
  el.textContent = JSON.stringify(trend.series, null, 2);
}
async function loadPanel(url, id, render) {
  try {
    render(await fetchJson(url));
  } catch (error) {
    showUnavailable(id, error);
  }
}
function refresh() {
  Promise.allSettled([
    loadPanel('/api/status', 'out-status', renderStatus),
    loadPanel('/api/alarms', 'out-alarms', renderAlarms),
    loadPanel('/api/trend?window_s=600', 'out-trend', renderTrend),
  ]);
}
async function act(path, payload) {
  const init = {method: 'POST', headers: {'Content-Type': 'application/json'}};
  if (payload) { init.body = JSON.stringify(payload); }
  const out = document.getElementById('out-action');
  try {
    const response = await fetch(path, init);
    const result = await response.json().catch(() => null);
    out.textContent = result === null
      ? '⚠ 操作失败：服务返回 ' + response.status
      : JSON.stringify(result, null, 2);
  } catch (error) {
    out.textContent = '⚠ 操作失败：' + (error && error.message ? error.message : error);
  }
  refresh();
}
window.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(refresh, 5000);
});
"""


def _nav(active: str, pages: tuple[str, ...]) -> str:
    items = []
    for page in pages:
        title = PAGE_TITLES.get(page, page)
        css = ' class="active"' if page == active else ""
        href = "/" if page == "press" else f"/{page}"
        items.append(f'<a href="{href}"{css}>{title}</a>')
    return "<nav>" + "".join(items) + "</nav>"


def _sections(page: str) -> str:
    """每个页面给出各自关注的重点字段。"""

    focused = {
        "press": "机群相位、在机模具与合模行程",
        "steam": "母管压力、疏水阀与主蒸汽阀开度",
        "timer": "硫化进度、曲线代际与完成闩锁",
        "alarms": "告警条目、审计摘要与落盘日志",
    }[page]
    return (
        "<section><h2>本页关注</h2>"
        f"<p style='font-size:13px;color:#d8b98c'>{focused}</p>"
        "<button onclick=\"act('/api/cure', {mold_id: 'mold-01'})\">硫化顺序</button>"
        "<button onclick=\"act('/api/open', {})\">开模顺序</button>"
        "<button onclick=\"act('/api/dispatch', {})\">机群调度</button>"
        "<button class='warn' onclick=\"act('/api/stop', {})\">停运顺序</button>"
        "<button onclick=\"act('/api/step', {dt: 1.0})\">推进周期</button>"
        "<pre id='out-action'>等待操作</pre></section>"
        "<section><h2>实时状态</h2><pre id='out-status'>加载中</pre></section>"
        "<section><h2>趋势窗口（10 分钟）</h2><pre id='out-trend'>加载中</pre></section>"
        "<section><h2>告警</h2><pre id='out-alarms'>加载中</pre></section>"
    )


def render_page(name: str, platform: Platform) -> str:
    """渲染一个控制台页面。"""

    if name not in PAGE_TITLES:
        raise KeyError(f"未知页面: {name}")
    pages = platform.config.pages
    title = PAGE_TITLES[name]
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{platform.config.name} · {title}</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<header><h1>"
        f"{platform.config.name} 控制台（{platform.config.project_id}）</h1>"
        f"{_nav(name, pages)}</header>"
        f"<main>{_sections(name)}</main>"
        f"<script>{_SCRIPT}</script></body></html>"
    )
