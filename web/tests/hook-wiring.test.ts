/**
 * `useChatStream` 的**接线契约**用例（离线，不渲染 React）。
 *
 * ## 为什么需要这样一组用例（坑 17）
 *
 * 现场报的故障是「一直显示正在连接、输入框点不动、点新会话就正常」。
 * 根因不是逻辑写错，而是**一处接线漏了**：创建连接的那个 `useEffect` 的依赖数组里
 * 没有 `connectionKey` —— 于是 `setConnectionKey` 改了一个**没人看**的 state，
 * 自动重连与「重连」按钮**都是哑的**，而唯一能"摆脱"卡住的办法变成了 `reset()`
 * （它只清 state，socket 没变）—— 正是用户描述的那个动作。
 *
 * 这类 bug 有个特点：**纯逻辑单测抓不到**（`stream.ts` 全绿）、**类型检查抓不到**
 * （依赖数组不是类型的一部分）、**真链路用例也抓不到**（Node 里没有 React）。
 * 所以这里用**源码级断言**把接线钉住 —— 与本仓 `tests/cache/test_cache_contracts.py`
 * 钉「缓存闸必须在检索之前」是同一手法（`13.cache.md` 的结构性契约）。
 *
 * ⚠️ 局限（写清楚，免得误读）：它检查的是**源码文本的形状**，不是运行期行为。
 * 它能挡住「有人把依赖项删了/忘了加」，挡不住「依赖项写了但逻辑顺序错」。
 * 真正的运行期覆盖需要浏览器（本步没有 Playwright，见 runbook §8 的接线点）。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOOK_SOURCE = readFileSync(path.resolve(HERE, "../src/components/useChatStream.ts"), "utf8");
const PAGE_SOURCE = readFileSync(path.resolve(HERE, "../src/app/page.tsx"), "utf8");

/** 取 `useEffect(...)` 到它的依赖数组之间的那段（拼在一起便于断言）。 */
function effectBody(source: string, marker: string): string {
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到 ${marker}`);
  // 取 marker 之后 4000 字符（足够覆盖一个 effect 的正文与依赖数组）
  return source.slice(start, start + 4000);
}

test("★建连 effect 的依赖里必须有 `connectionKey`（否则重连是哑的 —— 坑 17）", () => {
  // 这条就是那个 bug 的防回归：上一版依赖数组只有 [options.token, options.url]。
  // ⚠️ 判据是「**依赖数组**里有它」，不是「文件里某行同时出现 `}, [` 与 connectionKey」——
  // 后者会被依赖数组**上面的注释**骗过（注释里也提到了 connectionKey），
  // 于是这条用例连「故意去掉依赖」都抓不到（第一版就是这样，已修）。
  const body = effectBody(HOOK_SOURCE, "const handle = openChatStream({");
  const deps = body.match(/\},\s*\[([^\]]*)\]\);/);
  assert.ok(deps, "找不到建连 effect 的依赖数组（源码结构变了？）");
  const list = deps[1];
  assert.match(
    list,
    /connectionKey/,
    "依赖数组里没有 connectionKey：`setConnectionKey` 改的是没人看的 state，" +
      "自动重连与「重连」按钮都会失效（用户只能靠「新会话」清状态来摆脱卡住）",
  );
  assert.match(list, /options\.token/);
  assert.match(list, /options\.url/);
});

test("★`onOpen` 必须把排队的消息发出去（否则排队 = 静默丢弃）", () => {
  const body = effectBody(HOOK_SOURCE, "const handle = openChatStream({");
  const onOpen = body.slice(body.indexOf("onOpen:"), body.indexOf("onState:"));
  assert.match(onOpen, /pendingRef\.current/, "onOpen 里没有处理 pendingRef：排队的消息会在连上后丢失");
  assert.match(onOpen, /\.ask\(/, "onOpen 里没有调用 ask：排队的消息不会被发出");
});

test("★自动重连的次数用 ref 读，不能用 state（闭包里是旧值 → 会变成无限重连）", () => {
  const body = effectBody(HOOK_SOURCE, "const handle = openChatStream({");
  assert.match(body, /attemptsRef\.current < 2/, "没有用 ref 限制重试次数");
  assert.ok(
    !/connectionAttempts\s*<\s*2/.test(body),
    "用了 state（connectionAttempts）判断重试次数：它在 onState 闭包里永远是旧值（1），重试会无限次",
  );
});

test("★`running` 不能包含 connecting（否则连接卡住 = 输入框永久禁用 —— 坑 16/17）", () => {
  const line = HOOK_SOURCE.split("\n").find((item) => item.includes("const running ="));
  assert.ok(line, "找不到 running 的定义");
  assert.match(line, /phase === "streaming"/);
  assert.ok(
    !/connecting/.test(line),
    "running 里混进了 connecting：连接卡住时输入框会被永久禁用（用户既得不到回答也看不到原因）",
  );
  assert.match(HOOK_SOURCE, /const connecting =/, "应当单独暴露 connecting 给页面显示");
});

test("★`send` 在 connecting 时要**排队**，而不是丢弃、也不是直接塞进未就绪的连接", () => {
  const body = HOOK_SOURCE.slice(HOOK_SOURCE.indexOf("const send = useCallback"));
  const slice = body.slice(0, 1200);
  assert.match(slice, /phase === "connecting"/, "没有处理 connecting 分支");
  assert.match(slice, /pendingRef\.current = \{ text, sessionId \}/, "connecting 时没有把消息排进 pendingRef");
  // failed 时也要排队并换连接（否则用户得手点两次）
  assert.match(slice, /phase === "failed"/);
  assert.match(slice, /setConnectionKey\(\(key\) => key \+ 1\)/, "failed 时没有自动换连接");
});

/**
 * ★ 坑 20：`send` 判 `connecting` 就排队，**这个前提是「connecting 是暂时的」**。
 *
 * 而 `phase` 唯一能离开 `connecting` 的路径是「收到一帧」，后端又不会主动发帧 ——
 * 所以握手成功的那一刻**必须**显式推进 phase，否则整个页面自我锁死：
 * 排队 → 不发送 → 没有帧 → phase 不动 → 更不发送（实测：徽章「正在连接…」秒数一直涨、
 * 发消息永远没反应，而后端日志一切正常）。
 *
 * 这条把「推进」钉在**源码级**，与 `transport.test.ts` 的运行期用例互为双保险：
 * 运行期用例证明行为对，这条挡住有人把推进删掉（纯接线漏项正是坑 17 的同类）。
 */
test("★握手成功的 open 里必须推进 phase（否则 connecting 永久化 = 页面自我锁死 —— 坑 20）", () => {
  const streamSource = readFileSync(path.resolve(HERE, "../src/lib/stream.ts"), "utf8");
  const openHandler = streamSource.slice(
    streamSource.indexOf('socket.addEventListener("open"'),
    streamSource.indexOf('socket.addEventListener("message"'),
  );
  assert.ok(openHandler.length > 0, "找不到 socket 的 open 监听（源码结构变了？）");
  assert.match(
    openHandler,
    /push\(\{\s*\.\.\.state,\s*phase:\s*"idle"\s*\}\)/,
    "open 监听里没有把 phase 推到 idle：phase 只能靠「收到帧」离开 connecting，" +
      "而后端不会主动发帧 —— 结果是 send 永远排队、页面一条消息都发不出去（坑 20 实测故障）",
  );
});

test("页面：connecting 时给出「正在连接… Ns」与「重连」出口", () => {
  assert.match(PAGE_SOURCE, /正在连接…/, "页面没有显示握手中的计时");
  assert.match(PAGE_SOURCE, /chat\.connecting/, "页面没有读 connecting");
  assert.match(PAGE_SOURCE, /onClick=\{chat\.reconnect\}/, "页面没有「重连」按钮");
  // 「新会话」不许被当成唯一的逃生口：它只清 state（`reset` 的内部实现）
  const resetBody = HOOK_SOURCE.slice(HOOK_SOURCE.indexOf("const reset = useCallback"));
  assert.ok(
    !/setConnectionKey/.test(resetBody.slice(0, 400)),
    "reset 里居然换了连接：那会让「新会话」的作用超出它的语义（它只该清会话与消息）",
  );
});

test("`reconnect` 必须真的换连接（不只是清状态）", () => {
  const body = HOOK_SOURCE.slice(HOOK_SOURCE.indexOf("const reconnect = useCallback"));
  const slice = body.slice(0, 400);
  assert.match(slice, /setConnectionKey\(\(key\) => key \+ 1\)/, "reconnect 没有换连接");
  assert.match(slice, /waitingRef\.current = null/, "reconnect 没有清掉挂起的一轮（否则界面会一直转圈）");
});
