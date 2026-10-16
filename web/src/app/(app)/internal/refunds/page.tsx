"use client";

/**
 * 员工退款审批台（`/internal/refunds`）—— 变更 003 / 需求 3。
 *
 * ## 这一页解决什么
 *
 * 变更 003 之前，退款审批的落点是**对话轮闸门**：客户发起敏感操作 → 图挂起 →
 * **客户自己**在同一个页面上点「批准并继续」。那不是人机审批，是自审批 ——
 * 需求 2 报的「对客入口现在可以自己审批退款申请，明显有问题」指的就是它。
 *
 * 现在审批是一个**单据级**动作：这一页把 `refunds` 表摆出来，
 * 员工按退款单号 / 订单号 / 客户 id / 状态 / 时间范围筛，
 * 通过或驳回（`requested → approved / rejected`），并留下审批意见。
 *
 * ## 四条纪律
 *
 * ### ① 授权判据是 `approve:refund`，不是「能不能进对内」
 *
 * 这两个判据**不是**一回事（变更 003 决策 15）：
 *   * `canUseInternalEntry` = 能查全站订单 **或** 能审批退款 → 决定**进不进得了对内**；
 *   * `canApproveRefund` = 有 `approve:refund` → 决定**这一页能不能用**。
 * 合成一个判据的症状是：某个只有 `read:all_orders` 的角色看到这一页，
 * 而他每次点「通过」都被后端按 404 拒。
 *
 * ### ② 按钮禁用**不是**安全边界
 *
 * 真正的门是后端 `POST /api/refunds/decision` 上的 `@require_permission(PERM_APPROVE_REFUND)`。
 * 这一页只是不把「点不动的按钮」画出来。
 *
 * ### ③ 409 是**正常结果**，要引导刷新而不是让用户再点
 *
 * 两个员工同时打开待办列表时，后点的人会拿到 409（`refund_not_decidable`）——
 * 语义是「别人已经处理过了」。所以失败提示必须说「请刷新列表」，
 * 而不是笼统的「操作失败，请重试」（那会让他再点一次，再撞一次 409）。
 *
 * ### ④ 不接 LLM
 *
 * 变更 003 决策 23：审批是资金动作，按钮直连 REST 最可审计。
 * 「用一句话查退款」属另一条需求，不该把两件事混在一个界面里。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getRefunds, postRefundDecision } from "@/lib/api";
import { browserStorage, canApproveRefund, type Entry } from "@/lib/auth";
import { withFreshToken } from "@/lib/session";
import {
  type Refund,
  REFUND_STATUSES,
  refundStatusLabel,
} from "@/lib/contracts";
import { apiBase } from "@/lib/config";
import {
  describeApiFailure as describe,
  formatTime,
  MAX_NOTE_CHARS,
  refundBadgeClass,
} from "@/lib/refund_ui";
import { useSession } from "@/components/useChatStream";

const ENTRY: Entry = "internal";

/** 一页最多拉多少条（与后端 `MAX_LIMIT` 一致；前端不发明自己的上限）。 */
const PAGE_LIMIT = 100;

interface Filters {
  refundId: string;
  orderId: string;
  userId: string;
  statuses: string[];
  createdFrom: string;
  createdTo: string;
}

const EMPTY_FILTERS: Filters = {
  refundId: "",
  orderId: "",
  userId: "",
  statuses: [],
  createdFrom: "",
  createdTo: "",
};

export default function RefundReviewPage() {
  const { session, ready } = useSession(ENTRY);

  // ⚠️ 与其余页面同一条口径：**不做强制 redirect**（演示环境里能读的说明页更好排查）。
  if (!ready) return <div className="card muted">正在检查登录状态…</div>;
  if (!session) {
    return (
      <div className="card">
        <h1 className="card-title">退款审批：需要先登录</h1>
        <p className="muted">
          这一页调用的是审批接口（后端要求 <span className="mono">approve:refund</span> 权限点）。
          去 <Link href="/login">登录</Link>，用员工账号（客服 / 主管 / 审计 / 管理员）。
        </p>
      </div>
    );
  }
  if (!canApproveRefund(session.role)) {
    return (
      <div className="card">
        <h1 className="card-title">退款审批：当前身份没有审批权</h1>
        <p className="muted">
          你现在是 <span className="chip">{session.role}</span>（主体{" "}
          <span className="mono">{session.userId}</span>）。审批要求{" "}
          <span className="mono">approve:refund</span> 权限点。
          <br />
          ⚠️ 注意「能进对内入口」与「能审批退款」是**两个**判据：能进来不等于能批。
        </p>
        <div className="row">
          <Link className="btn ghost tiny" href="/internal">
            去数据查询
          </Link>
          <Link className="btn ghost tiny" href="/login">
            切换身份
          </Link>
        </div>
      </div>
    );
  }
  return <RefundConsole token={session.token} role={session.role} subject={session.userId} />;
}

function RefundConsole({ token, role, subject }: { token: string; role: string; subject: string }) {
  const [refunds, setRefunds] = useState<Refund[]>([]);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  /** 正在填审批意见的那一单（`null` = 没打开） */
  const [deciding, setDeciding] = useState<{ refund: Refund; note: string } | null>(null);

  const load = useCallback(
    async (active: Filters) => {
      setLoading(true);
      try {
        const payload = await withFreshToken(ENTRY, browserStorage(), (fresh) =>
          getRefunds(
            fresh,
            {
              refundId: numberOrUndefined(active.refundId),
              orderId: numberOrUndefined(active.orderId),
              userId: numberOrUndefined(active.userId),
              statuses: active.statuses,
              createdFrom: active.createdFrom || undefined,
              createdTo: active.createdTo || undefined,
              limit: PAGE_LIMIT,
            },
          ),
        );
        setRefunds(payload.refunds);
      } catch (exc) {
        setNotice({ kind: "error", text: describe(exc) });
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load(EMPTY_FILTERS);
  }, [load]);

  const applyFilters = () => {
    setNotice(null);
    void load(filters);
  };

  const resetFilters = () => {
    setFilters(EMPTY_FILTERS);
    setNotice(null);
    void load(EMPTY_FILTERS);
  };

  const decide = async (refund: Refund, decision: "approve" | "reject", note: string) => {
    setNotice(null);
    try {
      const updated = await withFreshToken(ENTRY, browserStorage(), (fresh) =>
        postRefundDecision(fresh, {
          refundId: refund.refund_id,
          decision,
          note: note.trim() || null,
        }),
      );
      setDeciding(null);
      // 决策之后必须**用当前筛选**重新拉一次：只改本地那一行会让「待审批」这个筛选
      // 显示一条已经批过的单（列表与筛选条件不一致，用户会以为筛坏了）。
      await load(filters);
      setNotice({
        kind: "ok",
        text: `退款单 ${updated.refund_id} 已${decision === "approve" ? "通过" : "驳回"}（当前状态：${refundStatusLabel(updated.status)}）。`,
      });
    } catch (exc) {
      setNotice({ kind: "error", text: describe(exc) });
    }
  };

  const pendingCount = useMemo(
    () => refunds.filter((item) => item.status === "requested").length,
    [refunds],
  );

  return (
    <div className="stack">
      <div className="card">
        <div className="row between">
          <div>
            <h1 className="card-title" style={{ marginBottom: 2 }}>
              退款审批
            </h1>
            <div className="small muted">
              后端 <span className="mono">{apiBase()}</span> · 身份{" "}
              <span className="chip">
                {subject} · {role}
              </span>{" "}
              · 本页 {refunds.length} 条、其中待审批 {pendingCount} 条
            </div>
          </div>
          <div className="row">
            <button className="btn ghost tiny" type="button" disabled={loading} onClick={() => void load(filters)}>
              {loading ? "加载中…" : "刷新"}
            </button>
            <Link className="btn ghost tiny" href="/internal">
              去数据查询
            </Link>
          </div>
        </div>
        <ul className="hint-list">
          <li>
            审批**只**能作用于「待审批」（<span className="mono">requested</span>）的单：
            已经批过/驳过的会被后端按 <span className="mono">409</span> 拒绝并带上当前状态。
          </li>
          <li>
            通过 = 记下「这笔退款被批准了」。本系统**不接支付通道**，也不会改订单状态——
            钱怎么退是另一件事（变更 003 的显式排除项）。
          </li>
          <li>
            审批人、角色与时间都由后端按**令牌**记录，不需要也不能在这里填。
          </li>
        </ul>
      </div>

      <div className="card">
        <h2 className="card-title">筛选</h2>
        <div className="row" style={{ marginTop: 8 }}>
          <input
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="退款单号"
            placeholder="退款单号"
            value={filters.refundId}
            onChange={(event) => setFilters({ ...filters, refundId: event.target.value })}
          />
          <input
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="订单号"
            placeholder="订单号"
            value={filters.orderId}
            onChange={(event) => setFilters({ ...filters, orderId: event.target.value })}
          />
          <input
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="客户 user_id"
            placeholder="客户 user_id"
            title="按客户内部 id 查（**不支持手机号**：那是 PII，且最低可见度的角色不该有存在性探测的工具）"
            value={filters.userId}
            onChange={(event) => setFilters({ ...filters, userId: event.target.value })}
          />
          <input
            className="input"
            style={{ maxWidth: 190 }}
            aria-label="创建时间下界"
            placeholder="创建起（2026-10-01）"
            value={filters.createdFrom}
            onChange={(event) => setFilters({ ...filters, createdFrom: event.target.value })}
          />
          <input
            className="input"
            style={{ maxWidth: 190 }}
            aria-label="创建时间上界"
            placeholder="创建止（2026-10-31）"
            value={filters.createdTo}
            onChange={(event) => setFilters({ ...filters, createdTo: event.target.value })}
          />
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          {REFUND_STATUSES.map((status) => (
            <label className="small" key={status} style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={filters.statuses.includes(status)}
                onChange={(event) => {
                  const next = event.target.checked
                    ? [...filters.statuses, status]
                    : filters.statuses.filter((item) => item !== status);
                  setFilters({ ...filters, statuses: next });
                }}
              />
              {refundStatusLabel(status)}
            </label>
          ))}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn" type="button" onClick={applyFilters}>
            查询
          </button>
          <button className="btn ghost" type="button" onClick={resetFilters}>
            清空条件
          </button>
          <button
            className="btn ghost tiny"
            type="button"
            onClick={() => {
              const pending = { ...EMPTY_FILTERS, statuses: ["requested"] };
              setFilters(pending);
              setNotice(null);
              void load(pending);
            }}
          >
            只看待审批
          </button>
        </div>
        <p className="small muted" style={{ marginTop: 6 }}>
          时间必须是 ISO8601（例如 <span className="mono">2026-10-01</span>）——
          写成「昨天」会被后端按 422 拒。**静默忽略非法筛选**是最坏的选择：
          用户会以为自己筛过了，看到的其实是全部数据。
        </p>
      </div>

      {notice ? (
        <div className={notice.kind === "ok" ? "card" : "error-box"}>
          {notice.kind === "ok" ? <span className="badge ok">成功</span> : null} {notice.text}
        </div>
      ) : null}

      {deciding ? (
        <div className="card">
          <div className="row between">
            <h2 className="card-title" style={{ marginBottom: 0 }}>
              审批退款单 <span className="mono">{deciding.refund.refund_id}</span>
            </h2>
            <button className="btn ghost tiny" type="button" onClick={() => setDeciding(null)}>
              取消
            </button>
          </div>
          <p className="small muted">
            订单 <span className="mono">{deciding.refund.order_id}</span> · 客户{" "}
            <span className="mono">{deciding.refund.user_id}</span> · 金额{" "}
            <strong>{deciding.refund.amount}</strong> 元 · 申请理由：
            {deciding.refund.reason ?? "（未填写）"}
          </p>
          <div className="row" style={{ marginTop: 8 }}>
            <input
              className="input"
              style={{ maxWidth: 460 }}
              aria-label="审批意见"
              placeholder="审批意见（可选，最长 255 字；会留档）"
              maxLength={MAX_NOTE_CHARS}
              value={deciding.note}
              onChange={(event) => setDeciding({ ...deciding, note: event.target.value })}
            />
            <button
              className="btn"
              type="button"
              onClick={() => void decide(deciding.refund, "approve", deciding.note)}
            >
              通过
            </button>
            <button
              className="btn ghost"
              type="button"
              onClick={() => void decide(deciding.refund, "reject", deciding.note)}
            >
              驳回
            </button>
          </div>
          <p className="small muted" style={{ marginTop: 6 }}>
            ⚠️ 这是**资金动作**：通过之后这笔退款记为已批准（不再占用可退额度的语义不变）。
            本系统不接支付通道，实际打款在另一套系统里完成。
          </p>
        </div>
      ) : null}

      <div className="card">
        <h2 className="card-title">退款单（{refunds.length}）</h2>
        <div className="scroll-x" style={{ marginTop: 10 }}>
          <table className="data">
            <thead>
              <tr>
                <th>退款单号</th>
                <th>订单号</th>
                <th>客户</th>
                <th>金额</th>
                <th>状态</th>
                <th>申请理由</th>
                <th>审批人</th>
                <th>审批角色</th>
                <th>审批意见</th>
                <th>申请时间</th>
                <th>审批时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {refunds.map((refund) => (
                <tr key={refund.refund_id}>
                  <td className="mono">{refund.refund_id}</td>
                  <td className="mono">{refund.order_id}</td>
                  <td className="mono">{refund.user_id}</td>
                  <td className="mono">{refund.amount}</td>
                  <td>
                    <span className={`badge ${refundBadgeClass(refund.status)}`}>
                      {refundStatusLabel(refund.status)}
                    </span>
                  </td>
                  <td className="cell-ellipsis" title={refund.reason ?? ""}>
                    {refund.reason ?? "—"}
                  </td>
                  <td className="mono small">{refund.decided_by ?? "—"}</td>
                  <td className="small">{refund.approver_role ?? "—"}</td>
                  <td className="cell-ellipsis" title={refund.decision_note ?? ""}>
                    {refund.decision_note ?? "—"}
                  </td>
                  <td className="small muted">{formatTime(refund.created_at)}</td>
                  <td className="small muted">{formatTime(refund.decided_at)}</td>
                  <td className="cell-actions">
                    <button
                      className="btn tiny"
                      type="button"
                      // 只有待审批的单可以决策：其余状态的按钮直接禁用（后端也会按 409 拒）
                      disabled={refund.status !== "requested" || deciding !== null}
                      title={
                        refund.status === "requested"
                          ? "通过或驳回这笔退款"
                          : `当前状态是 ${refundStatusLabel(refund.status)}，不能再审批`
                      }
                      onClick={() => {
                        setNotice(null);
                        setDeciding({ refund, note: "" });
                      }}
                    >
                      审批
                    </button>
                  </td>
                </tr>
              ))}
              {refunds.length === 0 ? (
                <tr>
                  <td colSpan={12} className="muted small">
                    {loading ? "加载中…" : "没有符合条件的退款单。"}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/** 空串 → `undefined`（不要把空输入当成 0 发出去）。 */
function numberOrUndefined(text: string): number | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const value = Number(trimmed);
  return Number.isFinite(value) && value > 0 ? value : undefined;
}
