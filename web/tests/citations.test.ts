/**
 * 出处串解析的单元测试（**离线、零依赖**）。
 *
 * 为什么这件事值得单测：`16.web.md` §8 的验收第 2 条是「RAG 回答带溯源展示」，
 * 而「展示」的入口就是把 `退货政策.md p.3#c12` 拆成三段。
 * 格式的真相在后端（`app/tools/retriever.py::RetrievedDoc.citation()`），
 * 它一旦变了，这里的用例会先红 —— 那正是我们要的（前端不会静默显示成一串看不懂的东西）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseCitation, parseCitations } from "../src/lib/contracts.ts";

test("完整出处串：文档 + 页码 + 片段", () => {
  const parsed = parseCitation("退货政策.md p.3#c12");
  assert.equal(parsed.document, "退货政策.md");
  assert.equal(parsed.page, 3);
  assert.equal(parsed.chunkId, "c12");
  assert.equal(parsed.raw, "退货政策.md p.3#c12");
});

test("只有文档名：不编页码、不编片段", () => {
  const parsed = parseCitation("物流政策.md");
  assert.equal(parsed.document, "物流政策.md");
  assert.equal(parsed.page, null);
  assert.equal(parsed.chunkId, null);
});

test("只有页码（没有片段）：页码单独可取", () => {
  const parsed = parseCitation("会员积分.md p.12");
  assert.equal(parsed.document, "会员积分.md");
  assert.equal(parsed.page, 12);
  assert.equal(parsed.chunkId, null);
});

test("只有片段 id", () => {
  const parsed = parseCitation("常见问题.html #chunk-7");
  assert.equal(parsed.document, "常见问题.html");
  assert.equal(parsed.page, null);
  assert.equal(parsed.chunkId, "chunk-7");
});

test("不可解析的串：**保留原文**，绝不返回空文档名", () => {
  // 展示层解析不出来是小事，让用户看不到来源是大事 —— 所以兜底必须是原文
  const parsed = parseCitation("某个来源（格式变了）");
  assert.equal(parsed.document, "某个来源（格式变了）");
  assert.equal(parsed.page, null);
  assert.equal(parsed.chunkId, null);
});

test("空串与空白：不抛异常", () => {
  assert.equal(parseCitation("").document, "");
  assert.equal(parseCitation("   ").document, "");
});

test("带页码和片段的时候，文档名里不该残留分隔符", () => {
  const parsed = parseCitation("账户安全与隐私.md p.2 #c3");
  assert.equal(parsed.document, "账户安全与隐私.md");
  assert.equal(parsed.page, 2);
  assert.equal(parsed.chunkId, "c3");
});

test("批量解析保持顺序与个数", () => {
  const parsed = parseCitations(["A.md p.1#c1", "B.txt", "C.html p.9#c9"]);
  assert.deepEqual(
    parsed.map((item) => item.document),
    ["A.md", "B.txt", "C.html"],
  );
  assert.deepEqual(
    parsed.map((item) => item.page),
    [1, null, 9],
  );
});

test("**实测的形状**：source 是相对仓库根的路径（`docs/kb/退货政策.md`）", () => {
  // 这一条不是假想：`rag_chunks.source` 实测存的是 `docs/kb/退货政策.md`，
  // 而 chunk_id 是 16 位 hex（`c00bcbd2da0b2ebe6`）。抄错形状的后果是
  // 「出处页请求 /api/kb?name=docs/kb/...」（被路由的路径校验拒掉，一片红）
  const parsed = parseCitation("docs/kb/退货政策.md #c00bcbd2da0b2ebe6");
  assert.equal(parsed.document, "退货政策.md");
  assert.equal(parsed.chunkId, "c00bcbd2da0b2ebe6");
  assert.equal(parsed.page, null);
});
