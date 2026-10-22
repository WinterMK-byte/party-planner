# -*- coding: utf-8 -*-
"""mcd_mcp.py —— 麦当劳 MCP 极简客户端（纯标准库，零第三方依赖）

为什么要自己写一个：
    比赛要求「真实使用麦当劳 MCP 能力」。但多数评委/用户 clone 下来不想先装 SDK。
    本文件只用 Python 标准库实现 MCP 的 Streamable HTTP 传输，`python mcd_mcp.py tools`
    一条命令就能验证连通，降低试用门槛。

协议要点（MCP over Streamable HTTP）：
    1. POST 同一个 URL，JSON-RPC 2.0 报文
    2. 首次 initialize，服务端可能在响应头返回 Mcp-Session-Id，后续请求带回
    3. 响应可能是 application/json，也可能是 text/event-stream（SSE，取 data: 行）
    4. notifications/initialized 是通知（无 id），服务端返回 202 空体

用法：
    set MCD_MCP_TOKEN=你的token        # Windows
    export MCD_MCP_TOKEN=你的token     # macOS / Linux

    python mcd_mcp.py check                    # 握手自检
    python mcd_mcp.py tools                    # 列出全部工具
    python mcd_mcp.py call list-nutrition-foods "{}"   # 调一个工具
    python mcd_mcp.py demo                     # 无网络/无 token 时的离线演示

环境变量：
    MCD_MCP_TOKEN   必填。在 https://open.mcd.cn/mcp 控制台申请
    MCD_MCP_URL     可选。默认 https://mcp.mcd.cn
    MCD_USE_PROXY   可选。设为 1 时走系统代理；默认直连
                    （有些沙箱会注入 HTTP_PROXY 拦截请求，直连更稳）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "mcd-skill"
CLIENT_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------
class McpError(RuntimeError):
    """MCP 调用失败。code 对应官方错误码：401 token 无效 / 429 触发限流。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _build_opener():
    """默认直连。理由：部分环境注入 HTTP_PROXY 会把请求拦成 502。"""
    if os.environ.get("MCD_USE_PROXY") == "1":
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _parse_body(raw: bytes, content_type: str) -> dict:
    """同时支持 application/json 与 text/event-stream 两种响应。"""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}

    if "text/event-stream" in (content_type or ""):
        # SSE：一行行找 data: 开头的，拼成完整 JSON
        chunks, buf = [], []
        for line in text.splitlines():
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
            elif line == "" and buf:
                chunks.append("\n".join(buf))
                buf = []
        if buf:
            chunks.append("\n".join(buf))
        for c in reversed(chunks):          # 取最后一条有效消息
            try:
                return json.loads(c)
            except json.JSONDecodeError:
                continue
        raise McpError(f"SSE 响应里没有可解析的 JSON：{text[:300]}")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise McpError(f"响应不是合法 JSON：{text[:300]}")


class McpClient:
    def __init__(self, token: str | None = None, url: str | None = None, timeout: int = 30):
        self.token = token if token is not None else os.environ.get("MCD_MCP_TOKEN", "")
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL)
        self.timeout = timeout
        self.session_id: str | None = None
        self._opener = _build_opener()
        self._id = 0

    # -- 底层 ------------------------------------------------------------
    def _post(self, payload: dict, *, expect_body: bool = True, retries: int = 2) -> dict:
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请先在 https://open.mcd.cn/mcp 申请，"
                "然后设置环境变量 MCD_MCP_TOKEN",
                code=401,
            )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None

        for attempt in range(retries + 1):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.session_id = sid
                    raw = resp.read()
                    ct = resp.headers.get("Content-Type", "")
                    if not expect_body or not raw:
                        return {}
                    return _parse_body(raw, ct)

            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 and attempt < retries:      # 限流：退避重试
                    time.sleep(1.5 * (attempt + 1))
                    last_err = McpError(f"429 触发限流，已重试 {attempt + 1} 次", code=429)
                    continue
                if e.code == 401:
                    raise McpError("401 Token 无效或已过期，请重新申请", code=401)
                raise McpError(f"HTTP {e.code}：{detail}", code=e.code)
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(1.0)
                    last_err = McpError(f"网络错误：{e.reason}")
                    continue
                raise McpError(f"网络不可达：{e.reason}")

        raise last_err or McpError("未知错误")

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload)
        if "error" in resp:
            err = resp["error"]
            raise McpError(f"{err.get('message', 'RPC 错误')}（code={err.get('code')}）")
        return resp.get("result", {})

    # -- 对外 ------------------------------------------------------------
    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        # 通知服务端握手完成（无 id、无返回体）
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_body=False,
        )
        return result

    def list_tools(self) -> list[dict]:
        result = self._rpc("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        # MCP 统一把结果放在 content[]，文本内容需自行拼接
        return result

    def call_tool_text(self, name: str, arguments: dict | None = None) -> str:
        """取工具返回的纯文本部分，多数数据型工具够用。"""
        result = self.call_tool(name, arguments)
        parts = []
        for item in result.get("content", []) or []:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# 离线快照：没有 token / 断网时保证项目仍能演示
# --------------------------------------------------------------------------
# 说明：这里的数值是「结构演示用占位数据」，不是官方实时结果。
# 字段名按 MCP 返回的常见结构书写，真实调用请以服务端返回为准。
DEMO_SNAPSHOT = {
    "_note": "离线演示数据，非官方实时结果。设置 MCD_MCP_TOKEN 后走真实 MCP。",
    "tools": [
        {"name": "list-nutrition-foods", "description": "餐品营养信息列表"},
        {"name": "query-meals", "description": "查询餐品"},
        {"name": "query-meal-detail", "description": "餐品详情"},
        {"name": "calculate-price", "description": "计算价格"},
        {"name": "query-nearby-stores", "description": "查询附近可用门店"},
        {"name": "query-store-coupons", "description": "查询门店优惠券"},
        {"name": "available-coupons", "description": "可用优惠券"},
        {"name": "auto-bind-coupons", "description": "自动绑定优惠券"},
        {"name": "campaign-calendar", "description": "活动日历"},
        {"name": "query-my-coupons", "description": "我的优惠券"},
        {"name": "now-time-info", "description": "当前时间信息"},
        {"name": "create-order", "description": "创建订单"},
        {"name": "query-party-city", "description": "派对城市"},
        {"name": "query-party-store", "description": "派对门店"},
        {"name": "query-partystore-date", "description": "派对可选日期"},
        {"name": "query-partystore-session", "description": "派对场次"},
        {"name": "party-order-create", "description": "派对下单"},
    ],
}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cmd_check(client: McpClient) -> int:
    print(f"接入地址: {client.url}")
    print(f"Token   : {'已设置（' + str(len(client.token)) + ' 字符）' if client.token else '未设置'}")
    try:
        info = client.initialize()
        print("握手    : 成功")
        print(f"  服务端   : {json.dumps(info.get('serverInfo', {}), ensure_ascii=False)}")
        print(f"  协议版本 : {info.get('protocolVersion')}")
        if client.session_id:
            print(f"  Session  : {client.session_id[:12]}…")
        tools = client.list_tools()
        print(f"工具数  : {len(tools)}")
        for t in tools:
            print(f"  - {t.get('name'):32s} {t.get('description', '')[:50]}")
        return 0
    except McpError as e:
        print(f"握手    : 失败 —— {e}")
        return 1


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2

    cmd = argv[1]
    if cmd == "demo":
        print(json.dumps(DEMO_SNAPSHOT, ensure_ascii=False, indent=2))
        return 0

    client = McpClient()
    try:
        if cmd == "check":
            return _cmd_check(client)

        if cmd == "tools":
            client.initialize()
            tools = client.list_tools()
            print(f"共 {len(tools)} 个工具")
            for t in tools:
                print(f"\n【{t.get('name')}】 {t.get('description', '')}")
                schema = t.get("inputSchema") or {}
                props = schema.get("properties") or {}
                req = set(schema.get("required") or [])
                for k, v in props.items():
                    mark = "*" if k in req else " "
                    print(f"   {mark}{k}: {v.get('type', '?')} — {v.get('description', '')[:60]}")
            return 0

        if cmd == "call":
            if len(argv) < 3:
                print("用法: python mcd_mcp.py call <tool-name> [json参数]")
                return 2
            name = argv[2]
            args = json.loads(argv[3]) if len(argv) > 3 else {}
            client.initialize()
            out = client.call_tool(name, args)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        print(f"未知命令: {cmd}")
        return 2
    except McpError as e:
        print(f"错误: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
