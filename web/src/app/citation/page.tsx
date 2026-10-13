"use client";

/**
 * 出处页（`/citation?q=退货政策.md p.3#c12`）：把出处串变成**可读的原文**。
 *
 * 它是 `16.web.md` §8 验收标准第 2 条（「RAG 回答带溯源展示」）落地时**必须**有的一页：
 * 只显示一串 `退货政策.md p.3#c12` 不叫溯源 —— 用户点不开、看不到原文，
 * 就无法自己判断「这句话是不是文档里说的」。所以这一页做三件事：
 *
 * 1. **解析**出处串（`lib/contracts.ts::parseCitation`，测试覆盖）→ 文档 / 页码 / 片段；
 * 2. 通过 `GET /api/kb?name=...` **读原文**（只读、白名单、限大小，见那条路由的注释）；
 * 3. 若有片段 id，**尝试定位**：语料是按 `### `/`## ` 分块的，`#c12` 这类编号由
 *    加载器生成（`scripts/load_knowledge_base.py`），拿不到精确映射时**如实说**
 *    「定位到最近的标题」—— 假定位比不定位更糟。
 */

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Suspense } from "react";
import { parseCitation } from "@/lib/contracts";

interface KbPayload {
  name?: string;
  content?: string;
  bytes?: number;
  modified?: string;
  documents?: string[];
  message?: string;
  error?: string;
}

/**
 * 从原文里找出全部标题（这一篇的结构：让用户知道片段来自哪一节）。
 *
 * ⚠️ **不导出**：`next build` 会对页面文件做入口检查，页面模块里除白名单
 * （`default` / `metadata` / `dynamic` …）之外的导出都会被判为「非法入口」
 * （实测报错：`Property 'headingsOf' is incompatible with index signature`）。
 * 想复用就放到 `src/lib/` 下 —— 这也符合「页面只做展示」的分层。
 */
function headingsOf(content: string): string[] {
  return content
    .split(/\r?\n/)
    .filter((line) => /^#{1,6}\s+/.test(line))
    .map((line) => line.replace(/^#{1,6}\s+/, "").trim());
}

function CitationView() {
  const params = useSearchParams();
  const raw = params.get("q") ?? "";
  const parsed = useMemo(() => parseCitation(raw), [raw]);
  const [payload, setPayload] = useState<KbPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!parsed.document) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const response = await fetch(`/api/kb?name=${encodeURIComponent(parsed.document)}`);
        const body = (await response.json()) as KbPayload;
        if (cancelled) return;
        if (!response.ok) {
          setError(body.message ?? `读取失败（HTTP ${response.status}）`);
          setPayload(body);
        } else {
          setPayload(body);
        }
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [parsed.document]);

  const headings = useMemo(() => (payload?.content ? headingsOf(payload.content) : []), [payload?.content]);

  return (
    <div className="stack">
      <div className="card">
        <h1 className="card-title">出处定位</h1>
        <div className="row">
          <span className="mono">{parsed.raw || "（没有给出处）"}</span>
        </div>
        <div className="row small" style={{ marginTop: 6 }}>
          <span className="badge accent">文档 {parsed.document || "解析不出"}</span>
          {parsed.page !== null ? <span className="badge">页码 p.{parsed.page}</span> : null}
          {parsed.chunkId ? <span className="badge">片段 #{parsed.chunkId}</span> : null}
        </div>
        {parsed.chunkId && payload?.content ? (
          <p className="small muted" style={{ marginBottom: 0 }}>
            片段编号由加载器（`scripts/load_knowledge_base.py`）生成，↔ 原文段落的映射不在接口里，
            所以这里**不做假定位**：下面按原文里最近的标题给你看整篇。
          </p>
        ) : null}
      </div>

      {loading ? <div className="card muted">正在读取 {parsed.document}…</div> : null}
      {error ? (
        <div className="error-box">
          {error}
          {payload?.documents && payload.documents.length > 0 ? (
            <div className="small" style={{ marginTop: 6 }}>
              语料目录里现有：{payload.documents.join("、")}
            </div>
          ) : null}
        </div>
      ) : null}

      {payload?.content ? (
        <div className="split">
          <div className="card">
            <div className="row between">
              <h2 className="card-title">{payload.name}</h2>
              <span className="small muted">
                {payload.bytes} 字节 · {payload.modified}
              </span>
            </div>
            <pre className="block" style={{ maxHeight: "58vh", whiteSpace: "pre-wrap" }}>
              {payload.content}
            </pre>
          </div>
          <div className="card">
            <h2 className="card-title">这一篇的标题结构</h2>
            <ul className="hint-list">
              {headings.length > 0 ? headings.map((item) => <li key={item}>{item}</li>) : <li>（没有标题）</li>}
            </ul>
            <div className="small muted" style={{ marginTop: 8 }}>
              另外 {payload.documents?.length ?? 0} 篇语料：
              {payload.documents?.map((item) => (
                <div key={item}>
                  <Link href={`/citation?q=${encodeURIComponent(item)}`}>{item}</Link>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function CitationPage() {
  // `useSearchParams` 必须包在 Suspense 里（Next 的静态预渲染要求），
  // 否则 `next build` 会以 "useSearchParams() should be wrapped in a suspense boundary" 失败
  return (
    <Suspense fallback={<div className="card muted">加载中…</div>}>
      <CitationView />
    </Suspense>
  );
}
