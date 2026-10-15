/**
 * 探活判据（变更 002 / T3）——**本模块存在的唯一理由是一个很容易写错的分支**。
 *
 * ## 为什么不能只看 `response.ok`
 *
 * 后端 `GET /health` **恒返回 200**，依赖故障写在 body 的 `status` / `components` 里。
 * 这是 `app/api/health.py` 刻意的设计（文档里写了两条理由）：
 *
 * 1. `/health` 同时被**存活探针**和**就绪探针**用。存活探针拿到非 2xx 会**重启进程**，
 *    而依赖（Postgres/Redis）不通时进程本身完全健康 —— 重启它只会把「依赖故障」
 *    放大成「全站重启 + 连接风暴」；
 * 2. 「进程真的死了」由**根本没响应**来表达，不需要状态码帮忙。
 *
 * 于是判据必须**两段**：
 *
 * ```text
 * ① fetch 抛异常（没有 HTTP 状态码）  → unreachable（后端没起 / 跨源被拦 / 地址写错）
 * ② fetch 拿到 200                   → 还得看 body.status：ok / degraded / down
 * ```
 *
 * ⚠️ **只写 ① 的用例会漏掉整个 ② 分支**。实测的假阴性长这样：Postgres 挂了，
 * 页面显示「后端正常」——因为 HTTP 确实是 200。所以本模块是纯函数、单独可测，
 * 用例里**必须有**一条「200 + status:"down"」（见 `tests/health.test.ts`）。
 *
 * ## 四态而不是三态
 *
 * `unreachable` 与 `down` **必须分开**：前者是「请求的结果没被读到」（先查进程起没起、
 * 再查跨源），后者是「后端活着但依赖不可达」（去查依赖栈）。把两者混成一句
 * 「后端异常」会把用户引到错误的排查方向上 —— 这与 `lib/diagnostics.ts` 的
 * 模块注释是同一套口径。
 */

import type { HealthPayload } from "./api.ts";
import { probeFailureHint } from "./diagnostics.ts";

/** 探活结论的四态（见模块注释：`unreachable` 与 `down` 不许混）。 */
export type HealthState = "ok" | "degraded" | "down" | "unreachable";

/** 一个**有问题**的组件（`ok` / `skipped` 的不进来）。 */
export interface ProblemComponent {
  name: string;
  status: string;
  detail: string;
  latencyMs: number | null;
}

export interface HealthJudgement {
  state: HealthState;
  /** 后端自己报的 `status`（`unreachable` 时为空串 —— 那时根本没读到 body） */
  reported: string;
  /** 非 ok 的组件（横幅要列出来，不能只说「异常」） */
  problems: ProblemComponent[];
  /** 一句话结论（横幅标题用） */
  summary: string;
  /** 分诊细节（多行；`unreachable` 时是 `probeFailureHint` 的完整排查步骤） */
  detail: string;
}

/**
 * 组件是不是「有问题」。
 *
 * `skipped` **不算问题**：它表示「这个依赖没启用」（例如没配 `REDIS_URL`、
 * 或 `OBSERVABILITY_ENABLED=false`）。把它算成异常，会让一个完全健康的开发环境
 * 每次打开页面都弹警告 —— 那正是 `/health` 的设计刻意避免的事。
 */
function isProblem(status: string): boolean {
  return status === "down" || status === "degraded";
}

function collectProblems(payload: HealthPayload): ProblemComponent[] {
  return payload.components
    .filter((item) => isProblem(item.status))
    .map((item) => ({
      name: item.name,
      status: item.status,
      detail: item.detail,
      latencyMs: item.latency_ms,
    }));
}

function describeProblems(problems: ProblemComponent[]): string {
  if (problems.length === 0) return "";
  return problems
    .map((item) => {
      const latency = item.latencyMs === null ? "" : `（${item.latencyMs}ms）`;
      return `· ${item.name}=${item.status}${latency} ${item.detail}`;
    })
    .join("\n");
}

/**
 * 把 `/health` 的响应体判成四态（**纯函数**，不碰网络）。
 *
 * `payload` 为 `null` 表示「没有读到 body」——调用方应改用 `unreachableJudgement()`；
 * 这里也兜底成 `unreachable`，免得调用方漏判后拿到一个假的 `ok`。
 */
export function judgeHealth(payload: HealthPayload | null): HealthJudgement {
  if (!payload) {
    return {
      state: "unreachable",
      reported: "",
      problems: [],
      summary: "后端没有响应：请求的结果没有被读到",
      detail: "",
    };
  }

  const problems = collectProblems(payload);
  const reported = String(payload.status ?? "");

  if (reported === "ok") {
    return {
      state: "ok",
      reported,
      problems,
      summary: `后端正常（env=${payload.env} version=${payload.version}）`,
      detail: "",
    };
  }

  if (reported === "down") {
    return {
      state: "down",
      reported,
      problems,
      summary: "后端依赖不可用（status=down）",
      detail: describeProblems(problems),
    };
  }

  if (reported === "degraded") {
    return {
      state: "degraded",
      reported,
      problems,
      summary: "后端降级运行（status=degraded）：业务多数可用，但有依赖不在最佳状态",
      detail: describeProblems(problems),
    };
  }

  // ⚠️ 认不出来的 status **不当成 ok**：这里宁可多报一次警，也不能把
  // 「后端改了一个我们不认识的状态词」静默显示成「一切正常」。
  return {
    state: "degraded",
    reported,
    problems,
    summary: `后端返回了无法识别的 status=${reported || "（空）"}（按降级处理）`,
    detail: describeProblems(problems),
  };
}

/**
 * 「请求根本没到达后端」的结论（`fetch` 抛异常时用）。
 *
 * 细节文案复用 `lib/diagnostics.ts::probeFailureHint` —— 它已经把
 * 「后端没起 / 地址写错 / 跨源被浏览器拦掉」三种原因区分开了，不在这里重写一遍。
 */
export function unreachableJudgement(exc: unknown, apiBase: string): HealthJudgement {
  return {
    state: "unreachable",
    reported: "",
    problems: [],
    summary: "后端没有响应：页面拿不到 /health（没有 HTTP 状态码）",
    detail: probeFailureHint(exc, { apiBase }),
  };
}

export interface ProbeResult {
  judgement: HealthJudgement;
  /** 后端原始响应（`unreachable` 时为 null）—— 横幅要显示 components，不能只给结论 */
  payload: HealthPayload | null;
}

/**
 * 执行一次探活并给出结论。
 *
 * ⚠️ 只做「取一次 + 判一次」，**不含任何定时器**：自动执行的那一次由
 * `HealthBanner` 的 `useEffect` 负责（页面打开/刷新各一次），
 * 手动那一次由「重新探活」按钮负责。把定时逻辑塞进来会让「探活为什么又跑了」
 * 变成一个查不清的问题。
 */
export async function probeHealth(
  getHealth: (options?: { signal?: AbortSignal }) => Promise<HealthPayload>,
  options: { apiBase: string; signal?: AbortSignal } = { apiBase: "" },
): Promise<ProbeResult> {
  try {
    const payload = await getHealth({ signal: options.signal });
    return { judgement: judgeHealth(payload), payload };
  } catch (exc) {
    return { judgement: unreachableJudgement(exc, options.apiBase), payload: null };
  }
}
