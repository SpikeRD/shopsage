"""`WS /api/chat/stream` 的**断连与忙等**用例（issue 007 的防回归）。

## 这个文件补的是既有用例够不到的那一类故障

`test_chat_stream.py` 覆盖的是「连接好好的时候协议对不对」；本文件覆盖的是
**「客户端在服务端还在跑图的时候断开」** —— 实测的故障正发生在那个窗口：

```text
用户在等 LLM 回复时点「退出登录」
  → 页面卸载 → 浏览器关掉 WS
  → 服务端图继续跑完（这是刻意的设计，见 chat.py 模块 docstring）
  → 跑完要发帧，而连接已经没了
```

### 故障机理（`py-spy` 实测的栈）

`websocket.receive_json()` 在连接不可用之后抛的是 **`WebSocketDisconnected`**
（`RuntimeError` 子类），而当时的消息循环只接住了 `WebSocketDisconnect`
（对端发来断开消息时的那个**另一个**异常），于是它落到兜底的 `except Exception`：

```text
① `_send_frame` 失败 → 它自己吞掉异常 → 返回
② `continue` → 下一次 `receive_json` 又**立刻**抛同一个异常（状态检查，不 await I/O）
③ 于是这是一个**纯 CPU 忙等**：单线程事件循环被这一条死连接占满一个核
④ 别的请求（含 `POST /api/auth/login`）永远得不到调度
```

用户视角 = **「退出登录之后再登录，一直显示登录中」**。

### ⚠️ 为什么这些用例既不起真连接、也不靠超时

**真连接**版本（`TestClient.websocket_connect` + 中途关闭）在回归时会把测试进程挂死：
那正是被测的故障本身。

**超时**（`asyncio.wait_for` / 看门狗线程）同样压不住它 —— 这是实测踩到的：
套了 2 秒超时，未修复的用例**仍然永久挂住**。原因是忙等循环里**没有任何 `await`
真正让出控制权**，所以事件循环的定时器回调**永远没有机会执行**，
`wait_for` 的超时根本不会触发。这一点本身就是 issue 007 值得记的认知：
「给外层加超时」不是这类 bug 的解药，**必须在代码层修掉**。

所以本文件改用**纯计数、零计时**的判据：假连接在第 N 次被调用时抛一个
**自定义的哨兵异常**（不是 `WebSocketDisconnected`）。于是：

* 修好了 → 第一次 `receive_json` 就返回，哨兵根本不会出现；
* 没修 → 循环转到第 N 次撞上哨兵，用例**立刻**拿到一条明确的失败信息。

两种情况下用例都在毫秒级结束，**不存在挂住的可能**。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnected

from app.api import chat as chat_mod
from app.api import deps as deps_mod
from app.security.auth import Principal

#: 假连接最多允许被调用几次。超过它 = 消息循环在忙等。
#:
#: 取值只需要「明显大于 1、又小到不会拖慢用例」：修好后是 **1**，
#: 没修则是无限次 —— 50 已经足够把两者分得一清二楚，且撞上哨兵只要几毫秒。
BUSY_LOOP_THRESHOLD = 50


class _BusyLoopDetected(BaseException):
    """消息循环转了太多次：说明它在忙等（issue 007）。

    ⚠️ **必须继承 `BaseException`（而不是 `Exception`）** —— 这是本用例能成立的关键，
    也是实测踩到的一处：第一版继承 `AssertionError`，结果哨兵被**被测代码自己的
    `except Exception` 兜底分支**接住了，然后 `continue` 继续转圈 —— 用例照样永久挂住。

    也就是说：**被测的那个兜底分支会吞掉任何 `Exception`**，所以判据只能从
    `Exception` 之外抛。这不是「为了测试而扭曲异常设计」：`BaseException` 的语义
    正是「控制流级别的信号，不该被业务兜底吞掉」（`KeyboardInterrupt`/`SystemExit`
    就是同一类），哨兵要表达的就是「循环已经跑飞了，立刻停下」。

    刻意**不继承 `AssertionError`**：那会是 `Exception` 的子类，会被吞掉（见上）。
    """


class _DeadWebSocket:
    """一个「连接已经不可用」的 WebSocket 替身，形状按 `chat_stream` 用到的成员来。

    与 Starlette 实测一致：`receive_json()` / `send_json()` 都**立刻**抛
    `WebSocketDisconnected`（状态检查，**不 await 任何 I/O**）—— 这正是忙等的燃料。

    额外做一件事：第 `BUSY_LOOP_THRESHOLD` 次收到 `receive_json` 时改抛
    `_BusyLoopDetected`（`BaseException`，**不会**被 `except Exception` 吞掉）。
    这把「无限忙等」变成一个**有限的、可断言的**事实。
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.receive_calls = 0
        self.send_calls = 0
        self.accepted = False
        self.app = _FakeApp()  # `chat_stream` 只从 `websocket.app.state` 取限流器
        self.state = _FakeState()

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None

    async def receive_json(self, mode: str = "text") -> Any:  # noqa: ARG002 - 与真签名一致
        self.receive_calls += 1
        if self.receive_calls >= BUSY_LOOP_THRESHOLD:
            raise _BusyLoopDetected(
                f"消息循环被调用了 {self.receive_calls} 次仍未结束："
                "连接不可用之后它在纯 CPU 忙等（issue 007）"
            )
        # ⚠️ 不 await：真实现就是「状态检查直接抛」。这个「不 await」正是忙等的成因之一。
        raise WebSocketDisconnected('WebSocket is not connected. Need to call "accept" first.')

    async def send_json(self, payload: Any) -> None:
        self.send_calls += 1
        raise WebSocketDisconnected('WebSocket is not connected. Need to call "accept" first.')


class _FakeState:
    request_id = "test-request-id"


class _FakeApp:
    class _State:
        rate_limiter = None  # 不限流：限流不是本条用例的被测对象

    state = _State()


async def _drive_dead_connection(websocket: _DeadWebSocket) -> None:
    """真调用 `chat_stream`，用一个已死的连接（鉴权/限流已替身掉）。"""
    await chat_mod.chat_stream(websocket)  # type: ignore[arg-type]


def test_message_loop_exits_when_connection_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """★连接已不可用时，`chat_stream` 必须**返回**，而不是继续转圈。

    这是 issue 007 的**行为级**防回归判据：驱动的是**真源码**（不是副本），
    且**零计时**（见模块 docstring：超时压不住这个忙等）。

    修复前：循环转到第 50 次 → 抛 `_BusyLoopDetected` → 用例红。
    修复后：第一次 `receive_json` 就返回 → 三条断言全过。
    """
    websocket = _DeadWebSocket()

    # 鉴权与限流不是本条用例的被测对象：替身掉，直接进消息循环。
    monkeypatch.setattr(deps_mod, "ws_subprotocols", lambda ws: [])
    monkeypatch.setattr(
        deps_mod,
        "ws_actor",
        lambda ws: Principal(user_id=2, role="customer", subject_type="customer", subject_id=2),
    )

    # ⚠️ 这里**故意不套 `asyncio.wait_for`**：忙等占满事件循环时它根本不会触发
    #    （实测），套上去只会给人一种「有保护」的错觉。判据在假连接里（计数）。
    asyncio.run(_drive_dead_connection(websocket))

    assert websocket.accepted, "鉴权通过后应当 accept（否则没走到消息循环）"
    assert websocket.receive_calls == 1, (
        f"连接不可用之后不许再收下一帧（实际收了 {websocket.receive_calls} 次）："
        "继续 continue 就是纯 CPU 忙等，会占满事件循环并让所有别的请求得不到响应"
    )
    # 修复后不该再尝试发任何帧（连接已经没了，发了也是白发）
    assert websocket.send_calls == 0, f"连接不可用时不该再发帧（发了 {websocket.send_calls} 次）"


# ── 结构级判据：异常分类必须在源码里看得见 ──────────────────────────────────


def test_chat_stream_catches_websocket_disconnected_separately() -> None:
    """★`WebSocketDisconnected` 必须被**单独**接住（issue 007 的源码级防回归）。

    判据是**结构**而不是行为：把那个 `except` 删掉就恢复成「滑进兜底分支 → 忙等」。

    ⚠️ **必须用 AST，不能用「源码里有没有这串字」** —— 实测踩到：第一版用
    `"except WebSocketDisconnected" in source`，把修复删掉后**它照样过**，
    因为文件里别处的**注释**提到了这个字符串（那段注释正是在解释这个 bug）。
    纯文本断言会被自己的说明文字骗过，这类「看起来在防回归、实际什么都没防」的
    用例比没有更糟 —— 它给人已经拦住的感觉。

    为什么结构断言在这里是**合适**的（而不是「测实现细节」）：
    * 这个 bug 的本质就是「**异常分类**」，而分类正确与否在 AST 里是**精确可判**的事实；
    * 行为判据（上面那条）已经覆盖了真源码，这条是**更便宜的第二道锁**。
    """
    tree = ast.parse(inspect.getsource(chat_mod.chat_stream).lstrip())
    # `getsource` 取到的是函数体（已 dedent 到 0 列），顶层就是一个 `with` 语句；
    # 直接 `ast.walk` 找「body 里有 receive_json 的那个 Try」即可。
    target: ast.Try | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        called = {
            sub.func.attr
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
        }
        if "receive_json" in called:
            target = node
            break

    assert target is not None, "没找到包含 receive_json 的 try 块（chat_stream 的结构变了？）"

    caught = {
        handler.type.id  # type: ignore[union-attr]
        for handler in target.handlers
        if isinstance(handler.type, ast.Name)
    }
    assert "WebSocketDisconnected" in caught, (
        "chat_stream 里 receive_json 所在的那个 try 必须单独接住 WebSocketDisconnected："
        "它表示「连接已不可用」，滑进兜底的 `except Exception` 会让消息循环变成"
        f"纯 CPU 忙等（占满事件循环 → 所有请求都得不到响应，issue 007）。实际接住的是 {caught}"
    )
    # 兜底分支仍然要在：那一帧可能确实是坏内容（连接是好的），不该被一起删掉。
    # ⚠️ 判据是「有一个 catch-all」：`except Exception` 是 `ast.Name`，
    #    裸 `except:` 才是 `None` —— 只认 `None` 会把现在这种写法误判成缺失（实测踩到）。
    def _is_catch_all(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return True  # 裸 `except:`
        return isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}

    assert any(_is_catch_all(handler) for handler in target.handlers), (
        "兜底的 `except Exception` 不该被删：连接受损与「这一帧内容不合法」是两件事，"
        "后者仍要回一条 bad_frame 错误帧并继续服务"
    )


def test_disconnected_and_disconnect_are_distinct_classes() -> None:
    """两个异常**不是**同一个类，所以只接住一个是不够的（修复的前提）。

    这条把「为什么要写两个 except」这个前提钉住：将来 Starlette 若把它们合并，
    这条会红、提醒去简化；反过来若有人以为 `WebSocketDisconnected` 是
    `WebSocketDisconnect` 的子类而删掉其中一个 `except`，这条会立刻指出来。
    """
    from starlette.websockets import WebSocketDisconnect

    assert not issubclass(WebSocketDisconnected, WebSocketDisconnect)
    assert not issubclass(WebSocketDisconnect, WebSocketDisconnected)
    assert issubclass(WebSocketDisconnected, RuntimeError)


def test_no_other_module_swallows_websocket_disconnects() -> None:
    """全仓只允许 `chat.py` 里那一处 WS 消息循环；别处新增的必须也接住断连。

    用 AST 找 `receive_json` / `receive_text` 的调用点：新增的 WS 循环若只写
    `except Exception`，就会复制出同一类忙等 —— 这条把那种复制挡在门外。
    """
    api_dir = pathlib.Path(chat_mod.__file__).parent
    offenders: list[str] = []
    for path in sorted(api_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"receive_json", "receive_text"}:
                continue
            if path.name == "chat.py":
                continue  # 唯一豁免：它已经被上面的断言单独钉住
            offenders.append(f"{path.name}:{node.lineno} 调用了 {name}")

    assert not offenders, (
        "除了 chat.py 之外出现了新的 WebSocket 消息循环，"
        f"请确认它也接住了 WebSocketDisconnected：{offenders}"
    )
