/**
 * **真链路**用例（需要后端在跑；没有令牌时整组跳过）。
 *
 * 它回答的是离线用例回答不了的那个问题：**我们抄的契约是不是真的**
 * （形状、帧顺序、字段名、关闭码）。所以它刻意用**同一个** `openChatStream()`
 * —— 浏览器里跑的就是这段代码，而不是另写一份「测试专用客户端」
 * （那种客户端会因为写得跟真客户端不一样而给出虚假的安全感）。
 *
 * 运行方式（见 runbook §6.3，一条命令同时签令牌 + 起前端 + 跑这组）：
 *
 * ```bash
 * node tests/run-live.mjs       # 或 npm run test:live（需要自己先备好环境变量）
 * ```
 *
 * 环境变量：
 *
 * | 变量 | 含义 |
 * |---|---|
 * | `SHOPSAGE_API_BASE` | 后端地址（默认 `http://127.0.0.1:8000`） |
 * | `SHOPSAGE_TOKEN_CUSTOMER` | 客户角色的 access token（**必须现签**，30 分钟过期） |
 * | `SHOPSAGE_TOKEN_INTERNAL` | 内部角色的 access token |
 * | `SHOPSAGE_TOKEN_EXPIRED` | 已过期的 access token（用来证「过期就是 4401」） |
 * | `SHOPSAGE_WEB_BASE` | 前端地址（默认 `http://127.0.0.1:3000`），用于探活 Next |
 */

import assert from "node:assert/strict";
import { after, test } from "node:test";

import { getHealth, postChat } from "../../src/lib/api.ts";
import { parseCitation } from "../../src/lib/contracts.ts";
import { openChatStream, toTurnResult, type StreamState } from "../../src/lib/stream.ts";
import { chartWarnings, toEChartsOption, toTableView } from "../../src/lib/chart.ts";
import { apiBase, streamUrl } from "../../src/lib/config.ts";

const API_BASE = process.env.SHOPSAGE_API_BASE ?? apiBase();
const WEB_BASE = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";
const TOKEN_CUSTOMER = process.env.SHOPSAGE_TOKEN_CUSTOMER ?? "";
const TOKEN_INTERNAL = process.env.SHOPSAGE_TOKEN_INTERNAL ?? "";
const TOKEN_EXPIRED = process.env.SHOPSAGE_TOKEN_EXPIRED ?? "";
const STREAM_URL = streamUrl("/api/chat/stream", API_BASE);

/** 跑一轮问答，等 `done`（或超时）。返回最终状态。 */
function askOnce(token: string, message: string, timeoutMs = 120_000): Promise<StreamState> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      handle.close();
      reject(new Error(`等 done 超时（${timeoutMs}ms）：${message}`));
    }, timeoutMs);
    let settled = false;
    const handle = openChatStream({
      token,
      url: STREAM_URL,
      onState: (state) => {
        if (state.phase === "failed" && !settled) {
          settled = true;
          clearTimeout(timer);
          handle.close();
          reject(new Error(`连接失败：${state.error?.message ?? "未知"}（close=${state.closeCode}）`));
          return;
        }
        if (state.phase === "done" && !settled) {
          settled = true;
          clearTimeout(timer);
          handle.close();
          resolve(state);
        }
      },
    });
    void handle.ready().then(() => handle.ask(message, `live-${Date.now().toString(36)}`));
  });
}

/** 等一次「被服务端关闭」，返回关闭码（用于 4401 这类断言）。 */
function closeCodeOf(token: string, timeoutMs = 20_000): Promise<number> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("等关闭超时")), timeoutMs);
    openChatStream({
      token,
      url: STREAM_URL,
      onState: () => {},
      onClose: (code) => {
        clearTimeout(timer);
        resolve(code);
      },
    });
  });
}

const skipNoCustomer = TOKEN_CUSTOMER ? false : "缺 SHOPSAGE_TOKEN_CUSTOMER（用 node tests/run-live.mjs 跑）";
const skipNoInternal = TOKEN_INTERNAL ? false : "缺 SHOPSAGE_TOKEN_INTERNAL";
const skipNoExpired = TOKEN_EXPIRED ? false : "缺 SHOPSAGE_TOKEN_EXPIRED";

test("后端在跑：/health 结构符合契约（HTTP 恒 200，依赖状态在 components 里）", async () => {
  const health = await getHealth();
  assert.equal(typeof health.status, "string");
  assert.ok(Array.isArray(health.components));
  assert.ok(
    health.components.some((item) => item.name === "postgres"),
    "components 里应当有 postgres（没有的话说明契约变了）",
  );
});

test(`前端在跑：${WEB_BASE} 能拿到页面（Next 的 HTML）`, async () => {
  const response = await fetch(WEB_BASE, { redirect: "manual" });
  assert.ok(response.status === 200 || response.status === 307, `HTTP ${response.status}`);
  const html = await response.text();
  assert.match(html, /ShopSage/, "页面里应当有品牌名（说明拿到的是我们的页而不是别的服务）");
});

test("对客页的两条 HTTP 契约：/api/kb 白名单与目录穿越拦截", async () => {
  const ok = await fetch(`${WEB_BASE}/api/kb?name=${encodeURIComponent("退货政策.md")}`);
  assert.equal(ok.status, 200);
  const payload = (await ok.json()) as { content?: string; documents?: string[] };
  assert.ok((payload.content ?? "").length > 0, "语料原文不该为空");
  assert.ok((payload.documents ?? []).includes("退货政策.md"));

  // 目录穿越必须被拒（400），而不是把仓库里的别的文件读出来
  const escape = await fetch(`${WEB_BASE}/api/kb?name=${encodeURIComponent("../.env")}`);
  assert.equal(escape.status, 400);
  const list = await fetch(`${WEB_BASE}/api/kb`);
  assert.equal(list.status, 200, "不带 name 时列文件清单");
});

test("★浏览器跨源这一层：后端必须放通前端来源（否则页面上只有 Failed to fetch）", async () => {
  // ## 为什么这条用例必须在这里（§7.6）
  //
  // 上面所有用例都是 **Node 的 fetch**，而 Node **不受 CORS 约束** —— 也就是说
  // 「这条测试文件全绿」完全不能证明浏览器能打开页面。实测警示（第 16 步）：
  // 后端当时一个 CORS 中间件都没有，Node 侧一切正常，而浏览器里
  // `GET /health` 只得到一句 `TypeError: Failed to fetch`（没有任何状态码）。
  //
  // Node 能验的部分：**带 `Origin` 头打进去，看响应里有没有 `Access-Control-Allow-Origin`**。
  // 这一条正是浏览器读响应所依赖的那个头。
  const origin = process.env.SHOPSAGE_WEB_BASE ?? "http://127.0.0.1:3000";

  const simple = await fetch(`${API_BASE}/health`, { headers: { Origin: origin } });
  assert.equal(simple.status, 200);
  assert.equal(
    simple.headers.get("access-control-allow-origin"),
    origin,
    "后端没回 Access-Control-Allow-Origin：浏览器会把这次跨源读拦掉（页面显示 Failed to fetch）。" +
      "修法：后端 .env 里 API_CORS_ORIGINS 加上这个来源并重启后端",
  );
  assert.match(
    simple.headers.get("access-control-expose-headers") ?? "",
    /x-request-id/i,
    "X-Request-Id 没暴露给浏览器：前端排障时读不到它",
  );

  // 预检：浏览器发 POST + Authorization 之前会先发它，而它**带不了令牌**
  const preflight = await fetch(`${API_BASE}/api/chat`, {
    method: "OPTIONS",
    headers: {
      Origin: origin,
      "Access-Control-Request-Method": "POST",
      "Access-Control-Request-Headers": "authorization,content-type",
    },
  });
  assert.equal(preflight.status, 200, "预检被拦了（不该要求鉴权）：浏览器会直接判定 CORS 失败");
  assert.equal(preflight.headers.get("access-control-allow-origin"), origin);
  const allowedHeaders = (preflight.headers.get("access-control-allow-headers") ?? "").toLowerCase();
  assert.ok(allowedHeaders.includes("authorization"), `预检没放通 Authorization：${allowedHeaders}`);

  // 错误响应也要带 CORS 头，否则前端连「401 未授权」都读不出来
  const unauthorized = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json" },
    body: JSON.stringify({ message: "hi" }),
  });
  assert.equal(unauthorized.status, 401);
  assert.equal(unauthorized.headers.get("access-control-allow-origin"), origin);

  // 不在白名单的来源不许拿到这个头（它是白名单，不是「谁问都给」）
  const evil = await fetch(`${API_BASE}/health`, { headers: { Origin: "http://evil.test" } });
  assert.equal(evil.headers.get("access-control-allow-origin"), null);
});

test("真流式：一帧不漏地走完 skeleton→node→answer→done（B2 的首帧 < 1s）", { skip: skipNoCustomer }, async () => {
  const state = await askOnce(TOKEN_CUSTOMER, "我的订单到哪了？");
  const events = state.seqs;
  assert.ok(events.length >= 5, `帧太少：${events.length}`);
  assert.deepEqual(
    events,
    Array.from({ length: events.length }, (_, index) => index + 1),
    "seq 必须从 1 连续递增（丢帧只可能来自本地逻辑）",
  );
  assert.equal(state.flowState === "delivered" || state.flowState === "blocked", true);
  assert.ok(state.answer.length > 0, "回答不能为空");
  // ⚠️ 变更 001 起前缀是**主体类别**：`c{user_id}` 客户 / `e{emp_id}` 员工。
  // 原来写的是 `startsWith("u")`（那时一个 `u` 对所有身份）——
  // 而那个形状正是「员工 emp_id=7 与客户 user_id=7 落进同一个会话桶」的根源。
  assert.ok(
    state.threadId.startsWith("c"),
    `thread_id 应当由服务端按主体派生（客户形如 c2:xxx），收到 ${state.threadId}`,
  );
  assert.ok(state.serverFirstFrameMs !== null && state.serverFirstFrameMs < 1000, `B2：首帧 ${state.serverFirstFrameMs}ms`);
  assert.ok(state.nodes.includes("router"), `节点帧里应当有 router，收到 ${state.nodes.join(",")}`);
});

test("真溯源：问知识库问题 → **答出来了** 且 answer 帧带出处、能解析、能点开", { skip: skipNoCustomer }, async () => {
  // ⚠️ 问法要挑一句**语料里真有、且路由稳定判成知识库类**的。
  // 实测对比（runbook §6.5）：
  //   "7天无理由退货的运费由谁承担？" → route=rag、3 条出处（稳定）
  //   "退货政策是怎么规定的？"        → 有时也走 rag，但检索回 0 条 → 无出处
  //
  // ## ⚠️ 这条用例曾经**只断言 citations**，于是在一条完全坏掉的路径上全绿（issue 009）
  //
  // 实测代价：语义缓存命中时 `rag_node` 返回 `docs: []`，输出护栏把缓存里的
  // **真答案**判成编造，用户拿到的是「抱歉，这个请求我无法处理」——
  // 而 `citations` **照常非空**（来自上一轮的真出处），所以这条用例当时 `pass 10 / fail 0`。
  //
  // 一句话记住：**「有出处」是必要条件，不是充分条件。**
  // 用户要的是「答出来了」，所以这里必须同时断言 `flowState === "delivered"`
  // 与回答正文里没有拦截话术 —— 那才是用户体感。
  //
  // 连问两次是刻意的：第 2 次必然走**语义缓存**，而 issue 009 正是只在第 2 次起发作。
  // 只问一次的用例，永远测不到缓存那条路。
  const question = "7天无理由退货的运费由谁承担？";

  for (const attempt of [1, 2]) {
    const state = await askOnce(TOKEN_CUSTOMER, question);

    // ① 真的答完了（用户体感的第一判据）
    assert.equal(
      state.flowState,
      "delivered",
      `第 ${attempt} 次提问没有答完（flow_state=${state.flowState}，blocked=${state.blocked}）：` +
        `回答=${state.answer.slice(0, 80)} —— 若这是第 2 次，先怀疑语义缓存那条路（issue 009）`,
    );
    assert.equal(state.blocked, false, `第 ${attempt} 次提问被拦下了`);
    assert.ok(state.answer.length > 0, `第 ${attempt} 次提问的回答不能为空`);
    // ② 回答正文里不能是拦截话术（比断言 flow_state 更贴近用户看到的东西）
    assert.ok(
      !/无法处理|被合规护栏拦下/.test(state.answer),
      `第 ${attempt} 次提问拿到的是拦截话术，不是答案：${state.answer.slice(0, 80)}`,
    );

    // ③ 溯源闭环：出处能到达前端、能解析、能点开
    assert.ok(
      state.citations.length > 0,
      `第 ${attempt} 次：知识库问题应当带出处（语料要先灌：uv run python -m scripts.load_knowledge_base；` +
        "若刚跑过全量回归，向量库已被清空 —— 见 runbook §10.1 坑 6）",
    );
    const parsed = parseCitation(state.citations[0]);
    assert.ok(parsed.document.length > 0, `出处解析不出文档名：${state.citations[0]}`);
    assert.ok(parsed.chunkId, `出处里应当带片段 id：${state.citations[0]}`);
    assert.match(parsed.chunkId, /^[0-9a-f]{6,}$/, `片段 id 形状不对：${parsed.chunkId}`);
    // 出处指向的文档必须真的能在 /api/kb 里读到（**闭环**：不然前端点开是一片 404）
    const response = await fetch(`${WEB_BASE}/api/kb?name=${encodeURIComponent(parsed.document)}`);
    assert.equal(response.status, 200, `出处页读不到 ${parsed.document}：HTTP ${response.status}`);
    const payload = (await response.json()) as { content?: string };
    assert.ok((payload.content ?? "").length > 0, "出处指向的原文不该为空");
  }
});

test("对内报表：HTTP 返回真 chart_spec，且能过图表适配层", { skip: skipNoInternal }, async () => {
  // ## 为什么这里要**重试到出图为止**（最多 3 次）
  //
  // 出图这条路线上有一段**非确定性**：`data` 节点的 SQL 由模型生成
  // （Text-to-SQL）。实测同一句话（"按订单状态统计订单数量"，已用
  // `scripts/probe_internal_query.py` 跑过 5 种句式筛出来）也会偶尔写成
  // 带中文占位符参数（`WHERE created_at >= :since`，而 `since="<统计起始时间，如 2024-01-01>"`）
  // 的 SQL，被 Postgres 以 `InvalidDatetimeFormat` 拒掉 → 没有数据行 → 没有图。
  //
  // ⚠️ 这条用例要断言的是**前端这一层**（「真 spec 能不能过适配层」），
  // 不是「模型今天稳不稳」。把模型的非确定性失败也算成前端用例的红，
  // 会让这组用例变成「看运气」，而看运气的用例最后会被人无视 ——
  // 那比没有用例更糟。所以：**给定最多 3 次机会，至少一次必须出图**，
  // 并对那次真 spec 做全部断言。模型稳定性本身属第 14 步评测的活（不是本步）。
  const attempts: string[] = [];
  let response: Awaited<ReturnType<typeof postChat>> | null = null;
  for (let index = 0; index < 3; index += 1) {
    const attempt = await postChat(TOKEN_INTERNAL, {
      message: "按订单状态统计订单数量",
      sessionId: `live-internal-${Date.now().toString(36)}-${index}`,
    });
    attempts.push(`${attempt.flow_state}${attempt.chart_spec ? "(有图)" : "(无图)"}`);
    if (attempt.chart_spec) {
      response = attempt;
      break;
    }
  }
  assert.ok(response, `连续 3 次都没出图 —— 这不是运气问题了，请查后端：${attempts.join(" / ")}`);
  const spec = response.chart_spec;
  assert.ok(spec);
  assert.equal(spec.schema, "shopsage.chart/v1");
  assert.ok(spec.data.length > 0, "图表数据不该为空");
  assert.ok(spec.series.length > 0);
  assert.ok(spec.x.field.length > 0, "x 轴列名不能为空");
  // 适配层必须能吃下真 spec（离线用例用的是抄来的 fixture，这里是真货）
  const table = toTableView(spec);
  assert.equal(table.columns.length, spec.series.length + 1);
  assert.equal(table.rows.length, spec.data.length);
  if (spec.type !== "table") {
    const option = toEChartsOption(spec);
    assert.equal(option.series.length, spec.series.length);
    assert.deepEqual(
      option.series.map((item) => item.name),
      spec.series.map((item) => item.name),
    );
  }
  assert.ok(Array.isArray(chartWarnings(spec)));
});

test("越权：客户角色问全站数据 → 不在前端崩，而是被后端如实拦住/降级", { skip: skipNoCustomer }, async () => {
  const state = await askOnce(TOKEN_CUSTOMER, "帮我查一下全站所有订单的总金额");
  assert.ok(state.flowState !== null, "无论放行还是拦下，都应当有一个明确的 flow_state");
  assert.ok(state.answer.length > 0, "用户总应当得到一句话");
});

test("过期令牌：握手被拒 → 关闭码 4401（前端据此提示重新登录）", { skip: skipNoExpired }, async () => {
  const code = await closeCodeOf(TOKEN_EXPIRED);
  assert.equal(code, 4401, `期望 4401，收到 ${code}`);
});

test("无令牌：同样 4401（而不是挂着不动）", async () => {
  const code = await closeCodeOf("");
  assert.equal(code, 4401, `期望 4401，收到 ${code}`);
});

after(() => {
  // 收尾：不留连接（openChatStream 的每个用例都自己关了；这里只是给一个显式的结束点）
});
