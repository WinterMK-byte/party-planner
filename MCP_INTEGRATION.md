# MCP 接入说明

## 1. MCP Server

| 项 | 值 |
|---|---|
| 服务 | 麦当劳中国 MCP Server |
| 地址 | `https://mcp.mcd.cn` |
| 开放平台 | <https://open.mcd.cn/mcp> |
| 传输 | Streamable HTTP |
| 认证 | `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | `2025-03-26` |
| 限流 / 错误码 | 600 次/分钟；`401` Token 无效、`429` 触发限流 |

客户端：`tools/mcd_mcp.py`（纯标准库，含 `initialize` / `tools/list` / `tools/call`，兼容 JSON 与 SSE 响应）。

## 2. 实际使用的 Tool

本项目的重点在于使用了**较少被使用的 `party` 系列**（团餐 / 派对预约）。

| Tool | 中文名 | 本项目用途 | 调用方式 |
|---|---|---|---|
| `query-party-city` | 派对城市 | 取可办派对的城市清单，按 `--city` 模糊匹配 | 无参 |
| `query-party-store` | 派对门店 | 取该城市可办派对的门店、地址、可容纳人数 | 轮询 `cityId` / `cityCode` / `city` |
| `query-partystore-date` | 派对可选日期 | 取该门店可选日期与当天场次数 | 轮询 `storeId` / `storeCode` / `partyStoreId` |
| `query-partystore-session` | 派对场次 | 取指定日期的场次、时间、人均、容量 | 同上 + `date` |
| `query-meal-assistance` | 助餐服务 | 预留（企业团餐场景），当前版本未启用 | — |
| `party-order-create` | 派对下单 | **刻意不调用**。见「设计取舍」 | — |

## 3. 调用流程

```
party_planner.py --city 上海 --people 8 --budget 80
  │
  ├─ initialize ────────────────────────────────▶ serverInfo
  ├─ tools/list ────────────────────────────────▶ 检查 party 系列工具是否存在
  │      └─ 缺工具 → 写入 schema_hint，最终在报告里提示用户
  │
  ├─ query-party-city {} ───────────────────────▶ [{name, cityId, cityCode}]（结构按实际情况）
  │      └─ 模糊匹配 --city → matched
  │
  ├─ query-party-store {cityId} ────────────────▶ 门店列表
  │      └─ 失败则依次换 {cityCode} / {city}
  │      └─ 提取 name / address / minPeople / maxPeople
  │
  ├─ query-partystore-date {storeId} ───────────▶ 可选日期
  │      └─ 提取 date / weekday / slots
  │
  ├─ query-partystore-session {storeId, date} ──▶ 场次
  │      └─ 提取 time / capacity / pricePerPerson / name
  │
  └─ render()：
        ├─ 门店表（按人数筛「是否匹配」）
        ├─ 日期表（标出已选日期）
        ├─ 场次表（逐条判定「✅ / ⚠ 超预算 / ⚠ 容不下」）
        ├─ 结论：挑出预算内 + 容量够的最便宜场次
        └─ 生成一段可复制到群里的通知文本
```

### 参数自适应策略

`party` 系列工具的入参命名可能随服务端迭代调整。本项目对每个调用准备**2–3 组候选参数**，按顺序尝试，第一个返回非空结果的即采用：

```python
store_args = [
    {"cityId": matched["id"]},
    {"cityCode": matched["code"]},
    {"city": matched["name"]},
    {"cityId": matched["id"], "cityCode": matched["code"]},
]
```

全部失败时**不伪造结果**，而是在报告末尾输出 `schema_hint`，提示用户运行 `python tools/mcd_mcp.py tools` 查看服务端真实 schema 后自行调整参数名。

## 4. 设计取舍：为什么不用 `party-order-create`

工具链里有下单能力，本项目**刻意不接**。

| 考虑的方案 | 问题 |
|---|---|
| 自动下单最便宜的场次 | 聚餐是多人决策。一个人替全场付钱，风险远大于效率收益 |
| 下单但只下 1 人份定金 | 语义不清，容易误操作 |
| **只生成方案 + 通知文案（本项目）** | 把决策权交回给人，工具只负责把信息摆清楚 |

同理，本项目没有实现「锁座」——那是对他人资源的占用，属于必须由人确认的动作。

## 5. 字段映射与容错

| 内部字段 | 探测的字段别名 |
|---|---|
| 城市名 | `name` / `cityName` / `城市` / `城市名称` |
| 城市 id | `id` / `cityId` / `cityCode` / `code` |
| 门店名 | `name` / `storeName` / `门店名称` |
| 门店地址 | `address` / `storeAddress` / `addr` |
| 容量 | `minPeople` / `minCapacity`、`maxPeople` / `maxCapacity` |
| 日期 | `date` / `dateStr` / `day` / `日期` |
| 星期 | `weekday` / `weekDay` / `week` |
| 场次时间 | `time` / `startTime` / `sessionTime` / `场次时间` |
| 人均价 | `price` / `pricePerPerson` / `amount` / `价格` |
| 剩余容量 | `capacity` / `maxPeople` / `remain` |

响应统一经 `as_list()` 归一化：支持 `content[].text` 拼成的 JSON 字符串（含被 markdown 围栏包裹的情况）、`dict` 里嵌 `data` / `items` / `list` / `stores` / `cities` / `dates` / `sessions`、以及直接返回 `list`。

## 6. 业务价值

| 价值 | 说明 |
|---|---|
| **把 4 次询问压缩成 1 条命令** | 门店 / 日期 / 场次 / 人均一次算完，组织者不用来回问 |
| **用到了一组冷门能力** | `party` 系列面向团餐与派对预约，多数作品只用了点餐与营养 |
| **覆盖真实且高频的社交场景** | 宿舍聚餐、社团庆功、班级聚会——比单人点餐更常有「谁来组织」的痛点 |
| **输出直接可执行** | 最后那段通知文案可以直接粘进群，省掉最烦的一步 |
| **风险可控** | 只读不写，不会产生任何订单或扣款 |

## 7. 自行验证

```bash
python tools/party_planner.py --demo --people 8 --budget 80
python tools/mcd_mcp.py check                 # 连通性
python tools/mcd_mcp.py tools                 # 看 party 系列的真实 schema
python tools/mcd_mcp.py call query-party-city "{}"
```

## 8. 合规

- Token 只从环境变量 `MCD_MCP_TOKEN` 读取，不硬编码、不进版本库。
- **不代为下单、不锁座、不扣款**，仅生成方案供人工确认。
- 门店、场次、价格与可预约状态以麦当劳官方渠道实时结果为准。
- 本项目为参赛作品，非麦当劳官方产品。
