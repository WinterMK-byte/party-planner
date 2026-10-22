# -*- coding: utf-8 -*-
"""party_planner.py —— 宿舍 / 班级聚餐，一键排期

「这周六晚上八个人，人均别超 80，去哪家麦当劳、几点、怎么跟群里说」

人肉做这件事要来回问三遍：谁有空 → 哪天 → 哪家店能办派对 → 人均多少。
本工具把这几步串起来，最后直接产出一段能贴进群里的通知。

用法：
    set MCD_MCP_TOKEN=xxx
    python party_planner.py --city 上海 --people 8 --budget 80
    python party_planner.py --city 上海 --people 8 --budget 80 --date 2026-10-25
    python party_planner.py --demo                        # 离线看效果
    python party_planner.py --city 上海 --people 8 --save plan.md

MCP 工具（party 系列）：
    query-party-city           有哪些城市可办派对
    query-party-store          该城市可办派对的门店
    query-partystore-date      该门店可选的日期
    query-partystore-session   某一天的场次
    party-order-create         下单（本工具只生成方案，不自动下单）
    query-meal-assistance      企业团餐助餐服务（非必须）

设计原则：
    - 只读不写：默认不调用 party-order-create，绝不替用户下单
    - 参数自适应：MCP 的入参名可能调整，这里按常见命名轮询尝试，
      并把真实 schema 打印出来供用户核对（mcd_mcp.py tools）
    - 无网可用：--demo 用内置示例跑通全流程
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcd_mcp import McpClient, McpError  # noqa: E402

DEMO = {
    "cities": [
        {"name": "上海", "id": "SH", "code": "021"},
        {"name": "北京", "id": "BJ", "code": "010"},
    ],
    "stores": [
        {"name": "麦当劳（徐汇滨江店）", "id": "SH-0142", "address": "龙腾大道 2879 号", "minPeople": 6, "maxPeople": 20},
        {"name": "麦当劳（五角场店）", "id": "SH-0087", "address": "淞沪路 77 号", "minPeople": 8, "maxPeople": 30},
        {"name": "麦当劳（人民广场店）", "id": "SH-0003", "address": "西藏中路 268 号", "minPeople": 10, "maxPeople": 40},
    ],
    "dates": [
        {"date": "2026-10-24", "weekday": "周六", "slots": 3},
        {"date": "2026-10-25", "weekday": "周日", "slots": 2},
        {"date": "2026-10-31", "weekday": "周六", "slots": 4},
    ],
    "sessions": {
        "2026-10-25": [
            {"time": "11:30", "capacity": 20, "pricePerPerson": 68, "name": "午市派对套餐"},
            {"time": "18:00", "capacity": 12, "pricePerPerson": 88, "name": "晚市派对套餐"},
        ]
    },
    "_note": "离线演示数据，非官方实时结果。设置 MCD_MCP_TOKEN 后重新运行可获取真实数据。",
}


def pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return default


def as_list(result) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, str):
        try:
            return as_list(json.loads(re.sub(r"^```[a-z]*\n|\n```$", "", result.strip())))
        except json.JSONDecodeError:
            return []
    if isinstance(result, dict):
        for k in ("data", "items", "list", "stores", "cities", "dates", "sessions", "result"):
            v = result.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                for k2 in ("list", "items", "data"):
                    if isinstance(v.get(k2), list):
                        return [x for x in v[k2] if isinstance(x, dict)]
        if any(k in result for k in ("name", "storeName", "cityName")):
            return [result]
    return []


def call_list(client: McpClient, tool: str, candidates: list[dict]) -> list[dict]:
    """按候选参数组合依次尝试，第一个返回非空的就用它。"""
    for args in candidates:
        try:
            text = client.call_tool_text(tool, args)
        except McpError as e:
            msg = str(e)
            if "401" in msg:
                raise
            continue
        got = as_list(text)
        if got:
            return got
    return []


def num(v, default=0.0):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else default


# --------------------------------------------------------------------------
# 在线流程
# --------------------------------------------------------------------------
def plan_live(client: McpClient, city: str, people: int, budget: float, want_date: str | None) -> dict:
    schema_hint: list[str] = []

    client.initialize()
    tools = {t.get("name"): t for t in client.list_tools()}
    for t in ("query-party-city", "query-party-store", "query-partystore-date", "query-partystore-session"):
        if t not in tools:
            schema_hint.append(f"服务端未提供工具 {t}")

    # ① 城市
    cities_raw = call_list(client, "query-party-city", [{}])
    cities = []
    for c in cities_raw:
        name = pick(c, "name", "cityName", "城市", "城市名称")
        if not name:
            continue
        cities.append({
            "name": str(name),
            "id": str(pick(c, "id", "cityId", "cityCode", "code", default="")),
            "code": str(pick(c, "code", "cityCode", default="")),
        })
    matched = next((c for c in cities if city in c["name"] or c["name"] in city), None)
    if not matched:
        raise McpError(f"没有找到城市「{city}」。可用城市：" + "、".join(c["name"] for c in cities[:10]))
    print(f"城市：{matched['name']}（id={matched['id']}）")

    # ② 门店
    store_args = [
        {"cityId": matched["id"]}, {"cityCode": matched["code"]},
        {"city": matched["name"]}, {"cityId": matched["id"], "cityCode": matched["code"]},
    ]
    stores_raw = call_list(client, "query-party-store", store_args)
    stores = []
    for s in stores_raw:
        name = pick(s, "name", "storeName", "门店名称")
        if not name:
            continue
        stores.append({
            "name": str(name),
            "id": str(pick(s, "id", "storeId", "storeCode", "code", default="")),
            "address": str(pick(s, "address", "storeAddress", "addr", default="")),
            "minPeople": int(num(pick(s, "minPeople", "minCapacity", default=0))),
            "maxPeople": int(num(pick(s, "maxPeople", "maxCapacity", default=0))),
        })
    print(f"可办派对门店：{len(stores)} 家")

    # ③ 日期 / 场次（默认取最近一家店）
    target_store = stores[0] if stores else None
    dates, sessions = [], {}
    if target_store:
        date_args = [
            {"storeId": target_store["id"]}, {"storeCode": target_store["id"]},
            {"partyStoreId": target_store["id"]},
        ]
        dates_raw = call_list(client, "query-partystore-date", date_args)
        for d in dates_raw:
            ds = str(pick(d, "date", "dateStr", "day", "日期", default=""))
            if not ds:
                continue
            dates.append({
                "date": ds,
                "weekday": str(pick(d, "weekday", "weekDay", "week", default="")),
                "slots": int(num(pick(d, "slots", "sessionCount", "availableSessions", default=0))),
            })
        # 只查目标日期的场次，避免无谓请求
        wanted = want_date or (dates[0]["date"] if dates else None)
        if wanted:
            sess_args = [
                {"storeId": target_store["id"], "date": wanted},
                {"storeCode": target_store["id"], "date": wanted},
                {"partyStoreId": target_store["id"], "date": wanted},
            ]
            for s in call_list(client, "query-partystore-session", sess_args):
                sessions.setdefault(wanted, []).append({
                    "time": str(pick(s, "time", "startTime", "sessionTime", "场次时间", default="")),
                    "capacity": int(num(pick(s, "capacity", "maxPeople", "remain", default=0))),
                    "pricePerPerson": num(pick(s, "price", "pricePerPerson", "amount", "价格")),
                    "name": str(pick(s, "name", "sessionName", "套餐名称", default="派对套餐")),
                })

    return {
        "source": "mcp", "city": matched, "stores": stores, "dates": dates,
        "sessions": sessions, "schema_hint": schema_hint, "target_store": target_store,
    }


def plan_demo(city: str, want_date: str | None) -> dict:
    city_row = next((c for c in DEMO["cities"] if city in c["name"]), DEMO["cities"][0])
    dates = DEMO["dates"]
    wanted = want_date or dates[0]["date"]
    sessions = DEMO["sessions"].get(wanted)
    if sessions is None:
        # 演示：给个假场次，保证流程跑完
        sessions = [
            {"time": "11:30", "capacity": 20, "pricePerPerson": 68, "name": "午市派对套餐（演示）"},
            {"time": "18:30", "capacity": 16, "pricePerPerson": 92, "name": "晚市派对套餐（演示）"},
        ]
    return {
        "source": "demo", "city": city_row, "stores": DEMO["stores"],
        "dates": dates, "sessions": {wanted: sessions},
        "schema_hint": [], "target_store": DEMO["stores"][0],
    }


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render(p: dict, people: int, budget: float, want_date: str | None) -> str:
    src = "麦当劳 MCP 实时" if p["source"] == "mcp" else "离线演示数据"
    L: list[str] = []
    L.append("# 聚餐方案 · " + p["city"]["name"])
    L.append("")
    L.append(f"> 人数 {people} 人 · 人均预算 ¥{budget:.0f} · 数据来源：{src}")
    L.append("")

    # 门店
    L.append("## 可选门店")
    L.append("")
    fit = [s for s in p["stores"] if (not s["minPeople"] or s["minPeople"] <= people)
           and (not s["maxPeople"] or s["maxPeople"] >= people)]
    if p["stores"]:
        L.append("| 门店 | 地址 | 可容纳 | 是否匹配人数 |")
        L.append("| --- | --- | --- | --- |")
        for s in p["stores"]:
            cap = (f"{s['minPeople']}–{s['maxPeople']} 人" if s["minPeople"] or s["maxPeople"] else "—")
            ok = "✅" if s in fit else "—"
            L.append(f"| {s['name']} | {s['address'] or '—'} | {cap} | {ok} |")
    else:
        L.append("（未查到可办派对的门店）")
    L.append("")

    # 日期
    L.append("## 可选日期")
    L.append("")
    if p["dates"]:
        L.append("| 日期 | 星期 | 可约场次 |")
        L.append("| --- | --- | --- |")
        for d in p["dates"]:
            mark = " ←已选" if want_date and d["date"] == want_date else ""
            L.append(f"| {d['date']}{mark} | {d['weekday'] or '—'} | {d['slots'] or '—'} |")
    else:
        L.append("（未查到可选日期）")
    L.append("")

    # 场次 + 预算判定
    L.append("## 场次与人均")
    L.append("")
    best = None
    any_budget_ok = False
    for date, sess in p["sessions"].items():
        L.append(f"**{date}**")
        L.append("")
        L.append("| 时间 | 套餐 | 可容纳 | 人均 | 预算 |")
        L.append("| --- | --- | --- | --- | --- |")
        for s in sess:
            ok = s["pricePerPerson"] > 0 and s["pricePerPerson"] <= budget
            cap_ok = (not s["capacity"]) or s["capacity"] >= people
            flag = "✅" if (ok and cap_ok) else ("⚠ 超预算" if not ok else "⚠ 容不下")
            L.append(f"| {s['time'] or '—'} | {s['name']} | {s['capacity'] or '—'} | ¥{s['pricePerPerson']:.0f} | {flag} |")
            if ok and cap_ok:
                any_budget_ok = True
                if best is None or s["pricePerPerson"] < best[1]["pricePerPerson"]:
                    best = (date, s)
        L.append("")

    # 结论
    L.append("## 结论")
    L.append("")
    if best:
        date, s = best
        total = s["pricePerPerson"] * people
        L.append(f"**推荐**：{date} {s['time']} · {s['name']} · 人均 ¥{s['pricePerPerson']:.0f} · "
                 f"{people} 人合计 ¥{total:.0f}")
        L.append("")
        L.append("### 可直接发群里的通知")
        L.append("")
        L.append("```text")
        L.append(f"【聚餐定档】{date}（{next((d['weekday'] for d in p['dates'] if d['date'] == date), '')}）{s['time']}")
        L.append(f"地点：{p['target_store']['name'] if p['target_store'] else '待定'}")
        L.append(f"人数：{people} 人｜人均约 ¥{s['pricePerPerson']:.0f}｜合计约 ¥{total:.0f}")
        L.append("AA 收款我这边统一走，迟到的补一份奶茶 🙂")
        L.append("能来的扣 1，来不了的扣 2")
        L.append("```")
    elif not any_budget_ok:
        L.append("**没有场次落在人均预算内。** 建议：① 提高预算 ② 换人数更少的日子 ③ 换一家门店再看。")
    else:
        L.append("**有可行场次但容量不足**，建议换门店或拆成两桌。")
    L.append("")

    if p.get("schema_hint"):
        L.append("## 需要注意")
        L.append("")
        for h in p["schema_hint"]:
            L.append(f"- {h}")
        L.append("- 可运行 `python tools/mcd_mcp.py tools` 查看服务端真实的工具与参数 schema。")
        L.append("")

    L.append("---")
    L.append("")
    L.append("> 本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品。")
    L.append("> **本工具只生成方案，不代为下单**；门店、场次、价格与可预约状态以麦当劳官方渠道实时结果为准。")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="宿舍/班级聚餐一键排期")
    ap.add_argument("--city", default="上海", help="城市名（默认 上海）")
    ap.add_argument("--people", type=int, default=8, help="人数（默认 8）")
    ap.add_argument("--budget", type=float, default=80, help="人均预算（默认 80）")
    ap.add_argument("--date", default=None, help="期望日期 YYYY-MM-DD")
    ap.add_argument("--demo", action="store_true", help="离线演示")
    ap.add_argument("--save", metavar="FILE", help="保存 Markdown")
    args = ap.parse_args(argv)

    try:
        if args.demo:
            p = plan_demo(args.city, args.date)
        else:
            client = McpClient()
            p = plan_live(client, args.city, args.people, args.budget, args.date)
    except McpError as e:
        print(f"[警告] {e}")
        print("       改为离线演示。设置 MCD_MCP_TOKEN 后重试可拿到真实场次。")
        p = plan_demo(args.city, args.date)

    md = render(p, args.people, args.budget, args.date)
    print(md)
    if args.save:
        io.open(args.save, "w", encoding="utf-8", newline="\n").write(md + "\n")
        print(f"\n已保存到 {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
