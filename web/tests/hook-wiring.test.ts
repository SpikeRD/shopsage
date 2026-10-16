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
// ⚠️ 变更 002 把页面移进了路由组 `(app)`（URL 不变，仍是 `/`）——
// 这里的路径必须跟着走，否则本文件会在 import 期就 ENOENT（整组用例直接不跑）。
const PAGE_SOURCE = readFileSync(path.resolve(HERE, "../src/app/(app)/page.tsx"), "utf8");

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

// ── 刷新令牌必须真的被接线（issue 003 的 P1-2）────────────────────────────────

/**
 * ★ 这一组防的是「**实现了、测试了、但没人调用**」这类漏项。
 *
 * P1-2 的真实形态：`ensureFreshToken` / `postRefresh` 写得完整、
 * `session.test.ts` 也覆盖了它们，但**全项目没有任何一处调用** ——
 * 于是访问令牌 30 分钟一到期，用户就被硬性踢回登录页，
 * 而刷新令牌（14 天有效）明明还躺在 localStorage 里。
 *
 * ⚠️ 单测**测不出**这种问题：单测直接调那个函数，当然能过。
 * 只有「源码里到底有没有调用点」这一层能发现它，所以这里做源码级断言。
 */
function readSource(relative: string): string {
  return readFileSync(path.resolve(HERE, relative), "utf8");
}

test("★`withFreshToken` 必须在真实调用点被使用（不只是定义 + 单测）", () => {
  // ⚠️ 只列**直接发起受保护请求**的文件。`(app)/page.tsx` 不在里面：
  //    它把令牌交给 `useChatStream`，由那个 hook 负责续期（见下一条断言）。
  const callSites = [
    "../src/app/(app)/internal/page.tsx",
    "../src/app/(app)/admin/page.tsx",
    "../src/components/useChatStream.ts",
    "../src/components/SideNav.tsx",
  ];
  for (const relative of callSites) {
    const source = readSource(relative);
    // ⚠️ 判据必须带 `(`：只写 `/withFreshToken/` 会被 **import 那一行**满足
    //    （`import { ..., withFreshToken, ... }`），于是「删掉调用、留着 import」
    //    这种最常见的漏项照样绿 —— 第一版就是这么写的，实测抓不到，已收紧。
    assert.match(
      source,
      /(withFreshToken|ensureFreshToken)\s*\(/,
      `${relative} 没有接上令牌续期：该文件的请求会在令牌过期后直接 401 ——` +
        "用户被踢回登录页，而刷新令牌还在本地（P1-2 的原始故障）",
    );
  }
});

test("★对客页把令牌交给 useChatStream，而那个 hook 必须负责续期", () => {
  // 对客页自己不发请求（它只用 WS + 审批），所以判据是「它走 hook」+「hook 会续期」。
  assert.match(
    PAGE_SOURCE,
    /useChatStream\(\{/,
    "对客页没有用 useChatStream：令牌续期的接线点就没了",
  );
  assert.match(
    HOOK_SOURCE,
    /ensureFreshToken|withFreshToken/,
    "useChatStream 没有接上续期：对客页的 WS 会用过期令牌握手（立刻 4401）",
  );
});

test("★建连前必须先续期令牌（WS 握手只发生一次，用过期的令牌会立刻 4401）", () => {
  const connectBody = HOOK_SOURCE.slice(HOOK_SOURCE.indexOf("const connect = async"));
  assert.ok(connectBody.length > 0, "找不到建连函数（源码结构变了？）");
  assert.match(
    connectBody.slice(0, 600),
    /ensureFreshToken/,
    "建连前没有续期令牌：握手只发生一次，用即将过期的令牌会立刻收到 4401 并断开",
  );
});

test("★`withFreshToken` 的 401 重试必须有次数上限（否则是死循环）", () => {
  const source = readSource("../src/lib/session.ts");
  const body = source.slice(source.indexOf("export async function withFreshToken"));
  const slice = body.slice(0, 1200);
  // 判据：只有**一次**重试调用（`return await run(refreshed)`），且刷新后比对令牌
  const retries = slice.match(/await run\(/g) ?? [];
  assert.equal(retries.length, 2, "应当是「原请求 + 最多一次重试」，多了就是死循环");
  assert.match(slice, /refreshed === token/, "刷新后没有比对令牌：同一条令牌重试只是白打一次");
});

// ── 事件值必须在**同步阶段**取出（issue 006）──────────────────────────────────

/**
 * ★ 这一组防的是「**受控输入 + 异步读 `event.target.value`**」这类时序 bug。
 *
 * issue 006 的真实形态：管理台改角色的 `<select>` 是受控的（`value={account.role}`）。
 * 用户选了 `manager` 之后：
 *
 * ```text
 * change 同步 = manager     ← 只有这一瞬间是对的
 * microtask   = customer    ← React 用旧的 account.role 把 DOM 回滚了
 * t+50ms      = customer
 * ```
 *
 * 而 `onChange` 里的 thunk 是**异步执行**的（`run()` 先 `setNotice(null)` 调度重渲染）。
 * 于是请求体发的是**旧角色**（等于什么都没改）→ 后端照旧 200 →
 * 而**同步求值**的提示语却说「已改为 manager」→
 * 用户看到「提示成功、实际没生效」。
 *
 * ⚠️ 纯逻辑单测**测不出**它（不渲染 React、没有受控组件回滚这种事）。
 * 所以这里做源码级断言：受控控件的事件值必须**先存进局部常量**，
 * 再交给异步 thunk 使用。
 */
test("★管理台改角色：必须同步取出事件值（受控 select 会在异步 thunk 执行前被回滚）", () => {
  const admin = readSource("../src/app/(app)/admin/page.tsx");
  const at = admin.indexOf("aria-label={`改 ${account.identifier} 的角色`}");
  assert.ok(at >= 0, "找不到改角色的 select（aria-label 改了吗？）");
  // 取这个 select 的 onChange 到收尾（足够覆盖 handler 全文）
  const handler = admin.slice(at, at + 1600);

  assert.match(
    handler,
    /const\s+nextRole\s*=\s*event\.target\.value/,
    "改角色的 onChange 没有把事件值先存进局部常量：" +
      "受控 select 会在异步 thunk 执行前被 React 回滚成旧值（issue 006 实测），" +
      "结果是「请求体发旧角色（没改）+ 提示语说改了」",
  );
  // 反向：thunk 里不许再读 `event.target.value`（那正是 bug 的形态）
  assert.ok(
    !/patchAccount[\s\S]{0,120}event\.target\.value/.test(handler),
    "`patchAccount` 的实参里仍在读 `event.target.value`：它是**异步**执行的，" +
      "那时受控 select 已经被回滚 —— 这正是 issue 006 的原始故障",
  );
});

test("★管理台：所有异步 thunk 里都不许直接读 `event.target.value`", () => {
  // 通用防线：`run(...)` 的 thunk 是异步的，受控控件的值在那时不可信。
  // 判据是「thunk 内部（`run(` 到它的收尾）没有 event.target.value」。
  const admin = readSource("../src/app/(app)/admin/page.tsx");
  const offenders = [];
  let cursor = admin.indexOf("void run(");
  while (cursor >= 0) {
    const chunk = admin.slice(cursor, cursor + 400);
    if (/event\.target\.value/.test(chunk)) offenders.push(chunk.slice(0, 120));
    cursor = admin.indexOf("void run(", cursor + 1);
  }
  assert.deepEqual(
    offenders,
    [],
    "这些 `void run(...)` 的异步 thunk 里直接读了 `event.target.value`：" +
      `\n${offenders.join("\n---\n")}\n` +
      "受控控件（input/select）的值必须**在 onChange 的同步阶段**先取出，" +
      "否则 React 已把它回滚成旧值（issue 006）",
  );
});

