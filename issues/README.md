# 问题记录（issues）

> **用途**：把「**实测撞到、但当场不修或修不完**」的问题**留痕**，而不是让它消失在聊天记录/日志里。
>
> 与 [`docs/runbook/`](../docs/runbook/) 的分工：
> * `docs/runbook/` 写的是**怎么做对**（正向操作手册）；
> * 本目录写的是**做不对时是什么样、为什么、要不要修**（问题台账）。
>
> 一条问题一个文件，命名 `NNN-简短英文名.md`（编号递增，不复用）。

## 什么时候该在此登记

满足任一条就登记：

* 复现了，但**当场不修**（要先做别的、或者要产品/环境决策）；
* 修了**一部分**，剩下的部分属于别的模块或需要外部条件；
* 现象**会反复咬人**（例如非确定性失败），需要一个固定的判据告诉后来者
  「这样是正常的、那样才是真的坏了」；
* 排查过程**推翻过假设**，值得留下「怎么排掉的」以免后人重走。

**不需要登记的**：当场就修好且没有残留的、纯属个人环境笔误的。

## 登记要求（每条必须能回答）

| 字段 | 说明 |
|---|---|
| **现象** | 别人照着能复现的那句话/那段输出（要贴真实报错，不要转述） |
| **影响** | 会不会挡住开工？会不会误导判断？ |
| **根因** | 查到哪一层就写到哪一层；**没查清就写"未定位"**，不要编 |
| **判据** | 怎么区分「正常」「异常」——这条最重要，防止后人误报 |
| **处理** | 已做什么 / 决定不做什么 / 还剩什么 |
| **状态** | `未处理` / `部分处理` / `已规避` / `已修复` |

---

## 台账

| # | 问题 | 状态 | 归属 |
|---|---|---|---|
| [001](001-internal-query-chart-nondeterminism.md) | 对内报表出图非确定性（5 句里偶有不出图）＋ 探针脚本报错不指向真因 | 部分处理 | 第 7 步 `analysis` 节点（prompt/解析健壮性） |
| [002](002-hf-cache-outside-workspace.md) | HF 模型缓存默认写仓库外 → 首次真检索抛 `PermissionError` | 已修复 | `app/llm/hf_cache.py`（第 5/10 步） |
| [003](003-security-and-correctness-fixes.md) | 实测发现的 2 个 P0 安全问题（登录限流失效 / SQL 行级过滤可绕过）与 3 个 P1 问题（缓存版本不推进 / 刷新令牌未接线 / 口令可设 1 字符） | 已修复 | `app/api/middleware.py`、`app/agents/agent_llm.py`、`app/cache/corpus.py`、`app/security/accounts.py`、`web/src/lib/session.ts` |
| [004](004-admin-cannot-create-account.md) | 管理员在管理台**开账号总是 422**：口令策略（≥12 位）只落在后端，前端一无所知、报错不说原因、失败还清空表单 | 已修复 | `web/src/app/(app)/admin/page.tsx`、`web/src/lib/password.ts` |
| [005](005-cors-missing-patch-method.md) | 管理台「停用 / 改角色」在浏览器里 `Failed to fetch`：CORS 白名单漏了 `PATCH` → 预检 400，请求根本没发出 | 已修复 | `app/api/main.py`（CORS 装配，第 16 步） |
| [006](006-admin-role-change-not-persisted.md) | 管理台「改角色」**提示成功但没生效**：受控 `select` 的值在异步 thunk 执行前被 React 回滚 → 请求体发的是旧角色 | 已修复 | `web/src/app/(app)/admin/page.tsx` |
| [007](007-logout-during-llm-hangs-event-loop.md) | **等 LLM 回复时退出登录 → 整个后端被一条死连接锁死**（WS 断连后 `WebSocketDisconnected` 掉进兜底分支 → 纯 CPU 忙等占满事件循环，再也登不进去、一直显示「登录中」） | 已修复 | `app/api/chat.py`（`WS /api/chat/stream` 消息循环，第 9 步） |
| [008](008-payment-method-masked-for-everyone.md) | `payment_method` 对**所有角色**（含财务级）都被打码成 `***`：脱敏表给它配了面向长数字串的 `mask_bank_card`，而真实值是 `alipay`/`wechat` 这类枚举（一个数字都没有）→ 走「位数不足→整体打码」分支 | 未处理 | `app/security/masking.py::FIELD_RULES`（第 4 步）；变更 003 使用现有规则、不改表 |
