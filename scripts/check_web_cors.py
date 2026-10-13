"""第 16 步 Web 端的**现场自检**：一条命令回答「两个进程都在吗、浏览器连得上吗」。

## 为什么需要它（而不是让人去猜）

第 16 步实测撞到过一个「两侧证据矛盾」的症状（`docs/runbook/16-web-runbook.md` §10.1 坑 11）：

```text
页面：连不上后端（http://127.0.0.1:8000）：Failed to fetch
后端日志：GET /health 200 OK          ← 服务是好的
```

成因是**跨源被浏览器拦掉**（后端当时没有 CORS 中间件）。这种症状最费时间的地方在于：
判断它需要同时看「前端进程在不在、后端进程在不在、后端放不放通前端来源」三件事，
而这三件事分散在三个地方（两个终端 + 一个配置文件）。

本脚本把这三件事**一条命令问完**，并且每一项都给「不通过时怎么办」：

```bash
uv run python -m scripts.check_web_cors
```

* 后端可达？→ 不可达就给起后端的命令
* 前端可达？→ 不可达就给起前端的命令
* 后端放通了前端的来源吗？（**这是浏览器唯一真正在意的一件事**）→ 没放通就给出要写进 `.env` 的那一行
* 预检（OPTIONS）答得掉吗？→ 答不掉说明 CORS 不在最外层（预检带不了令牌）

⚠️ 本脚本**不需要令牌、不需要语料**：它只回答连通性与跨源这两件事。
「回答里有没有溯源 / 图表」是别的问题（见同一个 runbook 的 §6）。
"""

from __future__ import annotations

import argparse
import sys

import httpx

DEFAULT_API = "http://127.0.0.1:8000"
DEFAULT_WEB = "http://127.0.0.1:3000"

#: 单项超时默认值：**给足「首次编译」的余量**。
#: `next dev` 是按路由现编译的（实测 `/` 首次 8.1s、`/internal` 11.7s、`/citation` 2s），
#: 所以 5 秒这种「看起来够快」的值会把「正在编译」报成「前端没起」——
#: 一个自查工具给出假信号比没有工具更糟（会让人去查一个不存在的问题）。
DEFAULT_TIMEOUT = 20.0


def _line(ok: bool, text: str) -> None:
    print(f"  {'✅' if ok else '❌'} {text}")


def _is_lan_address(origin: str) -> bool:
    """来源的 host 是不是「局域网/网卡地址」（不是回环）。

    为什么要单独提示这一类：`next dev` 自己会打印一行 `Network: http://<本机 IP>:3000`，
    照着那行打开页面的人就会用到 LAN 地址 —— 而它**不在**开发期默认白名单里
    （默认只有 localhost 与 127.0.0.1）。实测：LAN 来源的 HTTP 请求拿不到 CORS 头，
    而同一个地址上的 WebSocket 却能升级成功（WS 不受 CORS 约束）——
    于是症状是「界面能开、流式也能连、但一问就 Failed to fetch」，很难猜。
    """
    from urllib.parse import urlparse

    host = (urlparse(origin).hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


def check(api: str, web: str, *, timeout: float) -> int:
    """返回退出码：0 = 四项全通过；1 = 有项目不通过（可以直接用在脚本里做门禁）。"""
    failures = 0

    print(f"① 后端可达（{api}）")
    try:
        health = httpx.get(f"{api}/health", timeout=timeout)
        payload = health.json()
        _line(health.status_code == 200, f"HTTP {health.status_code} status={payload.get('status')} env={payload.get('env')}")
    except Exception as exc:  # noqa: BLE001 - 自检脚本，任何异常都只表示「这一项没过」
        failures += 1
        _line(False, f"连不上：{exc}")
        print(f"     → 起后端：uv run uvicorn app.main:app --host 0.0.0.0 --port 8000")

    print(f"② 前端可达（{web}）")
    try:
        page = httpx.get(web, timeout=timeout, follow_redirects=False)
        _line(page.status_code < 400, f"HTTP {page.status_code}（{len(page.content)} 字节）")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        _line(False, f"连不上：{exc}")
        print("     → 起前端：cd web; npm run dev")

    print(f"③ 后端放通前端来源吗（浏览器唯一真正在意的一件事）")
    print(f"   正在检查的来源：{web}")
    allowed_origin_ok = False
    try:
        simple = httpx.get(f"{api}/health", headers={"Origin": web}, timeout=timeout)
        allowed = simple.headers.get("access-control-allow-origin", "")
        allowed_origin_ok = allowed == web
        _line(allowed_origin_ok, f"Access-Control-Allow-Origin = {allowed or '（空）'}")
        if allowed != web:
            failures += 1
            print(f"     → 在后端 .env 里加一行：API_CORS_ORIGINS={web} 然后**重启后端**")
            print("       （开发期默认只放通 localhost:3000 与 127.0.0.1:3000；")
            print("        注意 localhost 与 127.0.0.1 是**两个不同来源**，要按你实际打开的地址配）")
            if _is_lan_address(web):
                # 这条很常见：`next dev` 自己会打印 `Network: http://<本机 IP>:3000`，
                # 照着那行打开页面就会用到 LAN 地址 —— 而它**不在**默认白名单里（实测）
                print("       ⚠️ 你检查的是一个「局域网/网卡地址」：`next dev` 会打印 `Network: http://<本机 IP>:3000`，")
                print("          照着它打开页面时就会走到这里。修法同上（把它写进 API_CORS_ORIGINS 并重启后端），")
                print("          或者改用 http://127.0.0.1:3000 打开（那一个默认就在白名单里）。")
        exposed = simple.headers.get("access-control-expose-headers", "")
        _line("x-request-id" in exposed.lower(), f"Access-Control-Expose-Headers = {exposed or '（空）'}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        _line(False, f"请求失败：{exc}")

    print("④ 预检（OPTIONS /api/chat，**不带令牌**）")
    try:
        preflight = httpx.options(
            f"{api}/api/chat",
            headers={
                "Origin": web,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
            timeout=timeout,
        )
        headers_ok = "authorization" in preflight.headers.get("access-control-allow-headers", "").lower()
        ok = preflight.status_code == 200 and headers_ok
        _line(ok, f"HTTP {preflight.status_code}（预检不该要求鉴权）")
        if not ok:
            failures += 1
            if not allowed_origin_ok:
                # ⚠️ 分支判断很重要（实测踩到）：来源不在白名单时，Starlette 也答 **400**，
                # 与「CORS 不在最外层导致预检撞上鉴权」的 401/429 不是一回事。
                # 混成一句「中间件位置不对」会把人引到错误的修法上。
                print("     → 根因是上面 ③：这个来源不在白名单里（来源不合格时预检本来就该失败）——")
                print("       先按 ③ 的修法把来源加进 API_CORS_ORIGINS 并重启后端，再回来看这一项")
            else:
                print("     → 预检被拦：CORS 中间件必须**最后加**（= 最外层），否则预检会先撞上鉴权/限流")
                print("       参考 app/api/main.py 里 add_middleware(CORSMiddleware, ...) 的位置与注释")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        _line(False, f"请求失败：{exc}")

    print("⑤ WebSocket 流式端点**真的能 upgrade** 吗（对客页的主链路）")
    ws_url = api.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/chat/stream"
    status, detail = _ws_probe(ws_url, origin=web, timeout=timeout)
    _line(status == "open" or status == "auth_rejected", f"{ws_url} → {detail}")
    if status not in {"open", "auth_rejected"}:
        failures += 1
        print("     → upgrade 没成功。两条最常见的原因：")
        print("       ① **URL 指到了前端自己**（如 ws://127.0.0.1:3000/...）：Next 的 dev server 不代理")
        print("          WebSocket upgrade —— 用 NEXT_PUBLIC_API_BASE 指到后端并重启前端；")
        print("       ② 中间有反代/防火墙没放通 upgrade（症状：后端日志里连握手都没有）。")
    elif status == "auth_rejected":
        print("       （未带令牌被拒是**正确**行为：说明 upgrade 通了、鉴权在正常干活）")

    print()
    if failures == 0:
        print("结论：两个进程都在、浏览器可以跨源读这个 API、WebSocket 能 upgrade —— 打开前端页面按 runbook §5 试。")
    else:
        print(f"结论：{failures} 项没过（上面每项的 → 后面就是修法）。")
    return 0 if failures == 0 else 1


def _ws_probe(url: str, *, origin: str, timeout: float) -> tuple[str, str]:
    """用**裸 socket** 发一次 WebSocket 握手，看服务端答的是 101 还是别的。

    为什么不用 `websockets` 客户端库：本脚本只依赖 httpx（各步骤都在用），
    而握手本身就是一段可读的 HTTP —— 自己能看清 `101`、`403`、`404`、超时四种结果，
    比库抛的异常更适合打印给人看（也不需要令牌：**未带令牌被 4401/401 拒，恰好证明 upgrade 通了**）。
    """
    import base64
    import os
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        f"Sec-WebSocket-Protocol: shopsage.v1\r\n"
        f"\r\n"
    )
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(handshake.encode())
            data = sock.recv(4096).decode("latin-1", errors="replace")
    except OSError as exc:
        return "unreachable", f"连不上（{exc}）"
    first = data.split("\r\n", 1)[0].strip()
    if " 101 " in first:
        return "open", f"{first}（upgrade 成功）"
    if " 401 " in first or " 403 " in first:
        # 未带令牌被拒是好事：说明 upgrade 通了、鉴权在正常干活
        return "auth_rejected", f"{first}（升级通、被鉴权拒 —— 符合预期）"
    if " 404 " in first:
        return "not_found", f"{first}（这个地址上没有 WebSocket 路由：URL 指错地方了？）"
    return "unexpected", f"{first or '（没有响应）'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 16 步 Web 端自检：进程 + 跨源（CORS）")
    parser.add_argument("--api", default=DEFAULT_API, help=f"后端地址（默认 {DEFAULT_API}）")
    parser.add_argument("--web", default=DEFAULT_WEB, help=f"前端来源（默认 {DEFAULT_WEB}）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=(
            f"单项超时（秒，默认 {DEFAULT_TIMEOUT}）。"
            "⚠️ 别设太小：`next dev` 的**首次**请求要现编译（实测 / 用 8.1s、其它路由 2~12s），"
            "超时太短会把「正在编译」报成「前端没起」——这正是自查工具自己最容易给假信号的地方"
        ),
    )
    args = parser.parse_args(argv)
    return check(args.api, args.web, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
