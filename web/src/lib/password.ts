/**
 * 口令策略的**前端镜像**（issue 004 修复点）。
 *
 * ## 为什么需要这个模块
 *
 * `issues/003` 的 P1-3 给口令加了强度校验（≥ 12 位、拒常见弱口令、拒与标识相同），
 * 但那份策略**只落在后端**（`app/security/accounts.py::validate_password_strength`）：
 * 管理台的口令框既没有 `minLength`、也没有说明文案，`create()` 只检查「非空」。
 *
 * 后果（实测，见 `issues/004`）：管理员填一个 8 位的口令点「开账号」，
 * 请求原样发出去 → 后端 422 → 页面只说「请求参数不合规」，
 * **唯一能自救的字段级原因被丢掉**，而表单还已经被清空了。
 * 「管理员无法创建账户」就是这么来的。
 *
 * ## 三条纪律
 *
 * 1. **后端始终是权威**：这里只是「更早失败 + 更早说清楚」，
 *    绝不因为前端放行就认为一定能建成功（第 ③ 条会把后端的原因翻出来）；
 * 2. **只镜像会静默变化的数字与两条一眼可判的规则**（长度、全空白、与标识相同）——
 *    **不抄**后端那份会持续增长的弱口令黑名单（抄一份必然漂移，
 *    而漂移的后果是「前端说没问题、后端拒」，比不抄更糟）；
 * 3. **报错不回显口令本身**（与后端 `validate_password_strength` 同源：
 *    凭据不该进日志/截图）。
 */

/**
 * 口令长度下限，与 `app/security/accounts.py::MIN_PASSWORD_LENGTH` **同值**。
 *
 * ⚠️ 这是**跨语言的重复**，所以 `web/tests/password.test.ts` 有一条用例
 * 直接去读那个 Python 文件里的数字比对 —— 只改一侧会让构建前就红，
 * 而不是等管理员点了「开账号」才发现。
 */
export const MIN_PASSWORD_LENGTH = 12;

/** 口令策略的纯函数实现：不合格返回**能照做**的一句话，合格返回 `null`。 */
export function passwordProblem(password: string, identifier = ""): string | null {
  const text = String(password ?? "");
  if (!text) return "口令不能为空。";
  if (text.length < MIN_PASSWORD_LENGTH) {
    return `口令至少 ${MIN_PASSWORD_LENGTH} 位（当前 ${text.length} 位）：短口令配合登录接口等于把账号交出去。`;
  }
  if (!text.trim()) return "口令不能全是空白字符。";
  if (identifier && text.trim().toLowerCase() === String(identifier).trim().toLowerCase()) {
    return "口令不能与登录标识相同。";
  }
  return null;
}

/**
 * 把后端的 422 字段错误（`detail.fields`）翻成一句**指明改哪里**的话。
 *
 * ## 为什么必须做这件事（issue 004 的根因 ②）
 *
 * 后端的 422 信封里**带着**字段级原因：
 *
 * ```json
 * {"error":{"code":"validation_error","message":"请求参数不合规",
 *   "detail":{"fields":[{"loc":["body","password"],
 *     "msg":"String should have at least 12 characters","type":"string_too_short"}]}}}
 * ```
 *
 * 但管理台的 `describe()` 只渲染 `message`，于是管理员看到的是
 * 「请求参数不合规」——他刚填的四个字段全是「参数」，不知道改哪个。
 *
 * ## 两条口径
 *
 * * **字段名翻成中文**（`password` → 「口令」），因为界面上就是这么写的；
 * * 认不出来时**原样透传** `msg`（不编、不吞）—— 那是 pydantic 的原话，
 *   比一句笼统的「参数不合规」有信息量，也不至于误导。
 */
export function fieldProblemText(detail: Record<string, unknown> | undefined): string | null {
  const fields = detail?.fields;
  if (!Array.isArray(fields) || fields.length === 0) return null;

  const parts: string[] = [];
  for (const item of fields) {
    const field = item as { loc?: unknown; msg?: unknown };
    const loc = Array.isArray(field.loc) ? field.loc.map((x) => String(x)) : [];
    // `loc` 形如 `["body", "password"]`：取最后一段当字段名（中间是 JSON 路径）
    const name = loc.length > 0 ? loc[loc.length - 1] : "";
    const raw = String(field.msg ?? "").trim();
    const label = FIELD_LABELS[name] ?? FIELD_LABELS[String(loc[1] ?? "")] ?? name;

    // 长度类错误换成前端自己的话术（与 `passwordProblem` 同一句，避免两套说法）
    if (label === "口令" && /at least \d+ characters/i.test(raw)) {
      parts.push(`口令至少 ${MIN_PASSWORD_LENGTH} 位。`);
      continue;
    }
    parts.push(label ? `${label}：${raw}` : raw);
  }
  return parts.length > 0 ? parts.join("；") : null;
}

/** 字段名 → 界面上用的中文标签（认不出来就用原字段名，不猜）。 */
const FIELD_LABELS: Record<string, string> = {
  identifier: "登录标识",
  password: "口令",
  new_password: "新口令",
  role: "角色",
  status: "状态",
  subject_type: "主体类别",
  subject_id: "主体 id",
};
