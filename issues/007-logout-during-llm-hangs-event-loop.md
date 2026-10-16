# 007 · 等 LLM 回复时退出登录 → **整个后端被一条死连接锁死**（再也登不进去，一直显示「登录中」）

* **状态**：已修复（2026-10-17）
* **发现于**：2026-10-17，用户反馈「在等待 llm 回复的过程中退出登录会导致无法再次登录，一直显示登录中」
* **归属模块**：`app/api/chat.py`（`WS /api/chat/stream` 的消息循环，第 9 步）
* **引入于**：第 9 步写这条消息循环时的形态就是漏的 —— 它不是某次改动引入的回归，
  而是**一开始就只接住了两个异常中的一个**（见「根因」）。之所以至今才暴露，
  是因为它需要「客户端在服务端还在跑图时断开」这个具体时序才触发

---

## 现象

用户在**等 LLM 回复的过程中点侧边栏「退出登录」**，之后：

```text
登录页点「登录」→ 按钮永远停在「登录中…」，不进也不报错。
```

**不是**这一个账号的问题：换账号、换浏览器、甚至直接 `curl` 登录，全部一样。后端表现为**整体失联**：

```text
GET /health                       → 永久超时（20s 都没有响应）
POST /api/auth/login              → 永久超时
```

### 判据（这句最关键）

* **「登录中…」一直转、且页面**没有任何**错误提示**（不是 401、不是 `Failed to fetch`）
  → 这**不是**账号/口令/令牌问题，而是**请求根本没被处理**。先用
  `py-spy dump --pid <后端进程>` 或看进程 CPU；
* **后端进程 CPU 打满一个核**（实测 5 秒增量 `5.38s`），且 `py-spy` 的栈停在
  `_send_frame → send_json → starlette.websockets.send` —— 这**不是**「等 LLM 等得久」，
  而是**单线程事件循环被一个纯 CPU 循环占满**；
* 反过来，「后端很忙但还活着」的判据是 `/health` **能**在几百毫秒内回 200。
  `/health` 超时 = 事件循环被饿死，**所有**请求（含登录）都不会被调度。

### 实测复现（三场景对照，定位到「断开」这一步）

```text
A. 只握手不发消息                    → /health 一直 200（20~60ms）       正常
B. 发 RAG 问题、**不断开**             → /health 一直 200（29~53ms）       正常，答完
C. 发 RAG 问题、2 秒后断开 WS          → /health **永久超时**             ← 故障
   [断开后] /health → **超时** (>5000ms)   ×12
```

也就是说：**触发条件是「断开」，不是「跑得慢」**。B 与 C 的差别只有那一次
`socket.close()`。

同一现象在真浏览器里复刻（`web/scripts/logout-during-llm-evidence.mjs`）：
登录 → 发问题 → 骨架已到、回答未到时点「退出登录」→ 回到 `/login` → 再登录失败。
⚠️ 注意真浏览器复刻**需要踩准窗口**：`_stream_turn` 是「先跑完图、再把节点帧一次性补发」，
所以「正在检索知识库…」这类节点文案出现时图**已经跑完**了 —— 那时再退出，
断开的是「回合已结束」的连接，踩不到故障。真正的窗口只有**首帧骨架到 answer 帧之间**那一段。

## 影响

* **一个用户的正常操作（退出登录）会打挂整个后端**：所有其他用户的请求一起失联。
  这是**可用性级别**的故障，不是「某个页面不好用」。
* **症状指向完全错误的方向**：「一直显示登录中」看起来像账号/令牌/前端状态问题，
  实际根因在**后端的 WebSocket 清理逻辑**，与登录接口本身毫无关系。
  不知道判据的人会去重置口令、重灌种子、清浏览器缓存 —— 全都是无效动作。
* **无法自愈**：进程不会恢复（实测等待 15 秒后仍然全部超时），只能重启进程。
* 与 issue 005/006 叠加时更难查：那两个的症状是「有 HTTP 状态码」或「提示成功却没生效」，
  而这个是**连接层完全无响应**。

## 根因

`app/api/chat.py::chat_stream` 的消息循环里，**「客户端断开」有两种异常，当时只接住了一种**。

Starlette 按两种完全不同的语义抛两个**互不继承**的异常（实测类层次）：

| 异常 | 语义 | 触发场合 |
|---|---|---|
| `WebSocketDisconnect`（`Exception` 子类） | 「收到了对端的断开消息」 | 客户端发了 close 帧 |
| **`WebSocketDisconnected`（`RuntimeError` 子类）** | **「这条连接已经不可用」** | 连接状态已离开 CONNECTED 之后的任何 `receive_*` / `send_*` |

```python
# 修复前的形态（只接住了第一个）
try:
    raw = await websocket.receive_json(mode="text")
except WebSocketDisconnect:
    return
except json.JSONDecodeError:
    ...                      # 这一帧不是合法 JSON：回 error 帧、continue
except Exception as exc:     # ← 兜底分支：WebSocketDisconnected 掉进了这里
    await _send_frame(websocket, seq, FRAME_ERROR, {"code": "bad_frame", "message": str(exc)})
    continue                 # ← 于是「继续收下一帧」
```

`WebSocketDisconnected` 是 `RuntimeError` 子类，**不是** `WebSocketDisconnect` 的子类，
所以它滑进了兜底分支。而兜底分支对「坏内容」的处理是**继续服务**，
用到「连接已死」上就成了一场灾难：

```text
① `_send_frame` 失败 → 它自己 `except Exception` 吞掉异常并返回（它本意是
   「发不出去不该打断正在跑的图」，这个设计本身是对的）→ 循环继续
② `continue` → 下一次 `receive_json` 又**立刻**抛同一个异常
③ ⚠️ 关键：这个异常是**状态检查直接抛**的，**不 await 任何 I/O**
   → 循环里没有任何让出点 → 这是一个**纯 CPU 忙等**
④ 事件循环是单线程的：这一条死连接占满一个核，
   其他所有请求（含 `POST /api/auth/login`）**永远得不到调度**
```

`py-spy dump` 的现场（局部变量把机制钉死了）：

```text
Thread 6980 (active+gil): "MainThread"
    _send_frame (app\api\chat.py:676)
        Arguments:
            seq: 31338568                                    ← 已经转了 3000 多万圈
            event: "error"
            data: {"code": "bad_frame", "message": "WebSocket is not connected. Need to call "accept" first."}
    chat_stream (app\api\chat.py:518)
        Locals:
            exc: <WebSocketDisconnected at 0x234b0b9e350>     ← 就是它
```

> 📌 **`seq: 31338568` 是这个 bug 最直白的一张照片**：那个计数器只在一轮问答里递增，
> 正常情况下一位数。它涨到三千万，说明这个循环已经**空转了几千万次**。

### 为什么「等 LLM」是触发它的时机（而不是别的时刻）

前端的退出登录是：清 `localStorage` → `window.location.assign("/login")`。
**页面卸载会让浏览器关掉那条 WebSocket**。而服务端的图**不会因此停下** ——
这是 `chat.py` 模块 docstring 刻意的设计（「客户端中途断开时图会继续跑完，
结果落进 checkpoint」）。于是：

```text
用户在等 LLM（图还有好几秒要跑）→ 退出登录 → 连接立刻断开
  → 但这几秒里没有人在收帧（图还在跑）
  → 图跑完了要发 answer/done 帧 → 发现连接没了
  → 进入上面那个忙等循环，**直到进程被杀**
```

如果用户在**图已经跑完**之后才退出（例如回答已经显示出来），断开时消息循环
正好阻塞在 `receive_json` 的**真** I/O 等待上，会正常收到 `websocket.disconnect`
→ 走 `WebSocketDisconnect` 那条分支 → 正常退出。**这就是为什么它只在
「等 LLM 的时候退出」才炸**，也是这个问题看起来如此诡异的原因。

### 为什么测试没发现

* 既有 `tests/api/test_chat_stream.py` 有 12 条用例，覆盖了坏 JSON、超长消息、
  无效 session_id、被拦、多轮、泄漏……**但全部是「连接一直好好的」**；
* 「客户端中途断开」这一类**一条都没有**。而它恰恰是最难写的：
  真连接版本一旦回归就会让 pytest **永久挂住**（忙等占满事件循环，
  连超时都走不到）—— `tests/api/conftest.py` 的看门狗注释记过这个代价；
* 服务端本身**没有 bug**：图照跑、状态照落 checkpoint，是**连接的收尾**漏了一个分支。

## 修复

`app/api/chat.py`：把 `WebSocketDisconnected` 与 `WebSocketDisconnect` **同等对待**
（连接没了就结束这一轮消息循环、正常返回），并保留兜底分支给「这一帧内容不合法」：

```python
try:
    raw = await websocket.receive_json(mode="text")
except WebSocketDisconnect:
    return
except WebSocketDisconnected:        # ★新增：连接已不可用（不是「内容不合法」）
    logger.debug("WS 连接已不可用，结束消息循环（user=%s）", principal.user_id)
    return
except json.JSONDecodeError:
    ...
except Exception as exc:             # 兜底仍保留：坏内容要回 error 帧并继续服务
    ...
```

> 📌 **判据是「连接不可用」还是「消息不合法」** —— 两者都表现为「收不到东西」，
> 但修法相反：前者必须**退出循环**，后者必须**继续服务**。把它们混进一个 `except`
> 正是这个 bug 的本质。

### 顺带记下的一条认知（比修复本身更值钱）

**这个忙等连 `asyncio.wait_for` 都压不住**。实测：给未修复的用例套上 2 秒超时，
它**仍然永久挂住** —— 因为忙等循环里没有任何 `await` 真正让出控制权，
事件循环的定时器回调**永远没有机会执行**，超时根本不会触发。

这解释了两件此前不好理解的事：

1. 为什么用户看到的是「**整个后端**都没反应」而不是「这一个请求有点慢」；
2. 为什么「给外层加超时/看门狗」**不是**这类 bug 的解药 —— 必须在代码层修掉。
   前端那条 180 秒的等帧看门狗（`lib/stream.ts`）在这里也完全帮不上忙：
   它保护的是**客户端**，而死者是**服务端的事件循环**。

## 验证

### 端到端（真连接 + 真后端，场景 C）

```text
修复前： 断开 WS 后 /health → **超时** (>5000ms) ×12，后接登录同样永久超时
修复后： 断开 WS 后 /health → HTTP 200 20ms / 87ms / 54ms / 45ms / 50ms / 60ms / 47ms …
```

### 真浏览器（`web/scripts/logout-during-llm-evidence.mjs`）

```text
PASS  客户登录后落在 /
PASS  点发送后进入「等 LLM 回复」的窗口（骨架已到、回答未到）
PASS  点退出登录后回到 /login
PASS  退出登录之后还能再登进去（不卡在「登录中…」）
全部通过（共 4 条）
```

### 防回归（`tests/api/test_chat_stream_disconnect.py`，新增 4 条）

| 判据 | 层级 | 摘掉修复后 |
|---|---|---|
| `test_message_loop_exits_when_connection_is_unavailable` | 行为（真调用 `chat_stream`） | **红**：`_BusyLoopDetected: 消息循环被调用了 50 次仍未结束…` |
| `test_chat_stream_catches_websocket_disconnected_separately` | 结构（AST 查 `except` 分类） | **红**：`实际接住的是 {'WebSocketDisconnect', 'Exception'}` |
| `test_disconnected_and_disconnect_are_distinct_classes` | 前提（两个类互不继承） | 通过（钉住修复的前提） |
| `test_no_other_module_swallows_websocket_disconnects` | 结构（AST 防第二处 WS 循环漏接） | 通过 |

实测（摘掉修复后重跑）：

```text
2 failed, 2 passed in 0.63s
FAILED tests/api/test_chat_stream_disconnect.py::test_message_loop_exits_when_connection_is_unavailable
FAILED tests/api/test_chat_stream_disconnect.py::test_chat_stream_catches_websocket_disconnected_separately
```

**这两条判据的设计本身就是本 issue 的一部分认知**（都是实测踩出来的）：

1. **不能用超时做判据**：第一版套了 `asyncio.wait_for(timeout=2)`，
   结果未修复时用例永久挂住（见上文「顺带记下的一条认知」）；
   最终改成**纯计数、零计时** —— 假连接在第 50 次被调用时抛一个**哨兵异常**；
2. **哨兵必须继承 `BaseException`**：第一版继承 `AssertionError`（`Exception` 子类），
   结果哨兵被**被测代码自己的 `except Exception` 兜底分支**接住，然后继续转圈 ——
   用例照样挂住。被测的那个分支会吞掉任何 `Exception`，所以判据只能从它之外抛；
3. **结构断言必须用 AST，不能用字符串匹配**：第一版写
   `"except WebSocketDisconnected" in source`，把修复删掉后**它照样过** ——
   因为文件里别处的**注释**提到了这个字符串（正是那段解释 bug 的注释）。
   这类「看起来在防回归、实际什么都没防」的用例比没有更糟。

## 遗留

* **`synthesis` 不是逐 token 产出**（既有已知边界，见 `chat.py` 模块 docstring）：
  「等 LLM」在用户侧是一段**没有任何中间反馈**的静默期（只有骨架话术）。
  本 issue 修的是「这段时间退出登录会炸后端」，**没有**改善那段时间的体感。
  要改善得靠第 11 步的增量帧，与本 fix 无关；
* **断连后的图仍在跑完**（刻意的设计，见模块 docstring）：本 fix 之后连接会干净退出，
  但这一轮仍然会把模型调用跑完（钱照花、checkpoint 照落）。
  「客户端走了就取消这一轮」是**另一个决策**（取消会让 checkpoint 停在半路，
  而「半路的会话」比「多花一次模型调用」贵得多）—— 不在本 issue 范围内。
