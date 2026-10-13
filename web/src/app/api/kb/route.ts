/**
 * 出处定位：`GET /api/kb?name=退货政策.md` → 知识库原文（只读）。
 *
 * ## 为什么前端要有一条「读知识库文件」的服务端路由
 *
 * 后端的 `citations` 只给出**出处串**（`退货政策.md p.3#c12`，
 * 见 `app/tools/retriever.py::RetrievedDoc.citation()`），**不返回片段正文**
 * —— 第 9 步的契约里没有「按出处取原文」的接口。而 `16.web.md` §4 第 3 条要求
 * 「展示『来源文档 + 片段』，可点击跳转」。两件事之间差的那一步就是这条路由：
 *
 * ```
 *   回答底部的出处串 ──▶ /citation?q=...（解析出文档名）──▶ 这条路由读原文 ──▶ 展示
 * ```
 *
 * ⚠️ **它读的是本仓 `docs/kb/` 下的语料文件**（知识库的源文档就在仓库里）。
 * 也就是说这是一条**只读的、面向本仓语料的**路由，不是通用的文件服务。
 *
 * ## 安全（三条，缺一不可）
 *
 * 1. **目录穿越必须堵住**：`name` 只允许「一层、不带路径分隔符、不以点开头」的文件名，
 *    并且解析之后**再校验一次真实路径仍在 `docs/kb` 之下**（两道，因为只做字符串检查
 *    容易被各种编码绕过）；
 * 2. **扩展名白名单**：只读 `.md` / `.txt` / `.html`（语料就是这三种）。
 *    白名单而不是黑名单 —— 与后端 `4.security.md` 的「表/列白名单」同一条纪律；
 * 3. **大小上限**：超过 256KB 直接拒（避免把整个仓库当语料读出去）。
 */

import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

/** 语料目录（相对仓库根；`web/` 的上一级就是仓库根）。 */
const KB_DIR = path.resolve(process.cwd(), "..", "docs", "kb");

const ALLOWED_EXTENSIONS = new Set([".md", ".txt", ".html"]);
const MAX_BYTES = 256 * 1024;

function sanitize(name: string): string | null {
  const value = String(name ?? "").trim();
  if (!value) return null;
  if (value.length > 200) return null;
  // 只允许一层文件名：不许有路径分隔符、不许 `..`、不许以点开头
  if (value.includes("/") || value.includes("\\") || value.includes("..") || value.startsWith(".")) return null;
  const extension = path.extname(value).toLowerCase();
  if (!ALLOWED_EXTENSIONS.has(extension)) return null;
  return value;
}

async function listDocuments(): Promise<string[]> {
  try {
    const entries = await readdir(KB_DIR);
    return entries.filter((entry) => ALLOWED_EXTENSIONS.has(path.extname(entry).toLowerCase())).sort();
  } catch {
    return [];
  }
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const requested = url.searchParams.get("name");

  if (!requested) {
    return NextResponse.json({ documents: await listDocuments(), dir: KB_DIR });
  }

  const name = sanitize(requested);
  if (!name) {
    return NextResponse.json(
      {
        error: "invalid_name",
        message: "只允许读取 docs/kb 下的一层语料文件（.md/.txt/.html），且不含路径分隔符",
      },
      { status: 400 },
    );
  }

  const absolute = path.resolve(KB_DIR, name);
  // 第二道：解析之后的真实路径必须仍在 KB_DIR 之内（防 `%2e%2e` 之类的花样）
  if (!absolute.startsWith(KB_DIR + path.sep)) {
    return NextResponse.json({ error: "invalid_name", message: "路径越界" }, { status: 400 });
  }

  try {
    const info = await stat(absolute);
    if (!info.isFile()) {
      return NextResponse.json({ error: "not_a_file", message: "不是文件" }, { status: 404 });
    }
    if (info.size > MAX_BYTES) {
      return NextResponse.json(
        { error: "too_large", message: `文件超过 ${MAX_BYTES} 字节上限` },
        { status: 413 },
      );
    }
    const content = await readFile(absolute, "utf8");
    return NextResponse.json({
      name,
      bytes: info.size,
      modified: info.mtime.toISOString(),
      content,
      documents: await listDocuments(),
    });
  } catch {
    return NextResponse.json(
      { error: "not_found", message: `语料目录里没有这个文件：${name}`, documents: await listDocuments() },
      { status: 404 },
    );
  }
}
