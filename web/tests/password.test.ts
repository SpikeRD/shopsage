/**
 * 口令策略的**跨层契约**用例（issue 004 的防回归）。
 *
 * ## 为什么需要这样一组用例
 *
 * issue 004 的真实形态：后端有一个明确的策略（≥ 12 位），前端**完全不知道它的存在**
 * —— 管理台的口令框没有 `minLength`、没有文案、`create()` 只判「非空」，
 * 于是管理员填 `pass1234` 点「开账号」必然 422，而页面只说「请求参数不合规」。
 *
 * 这类 bug 的特点与本仓「坑 17」（接线漏了）同源：
 * **两侧各自的用例都是绿的**（后端用例自己填够长的口令、前端离线用例不渲染 React），
 * 坏的是**中间那条缝**。所以这里做三件事：
 *
 * | 判据 | 挡住什么 |
 * |---|---|
 * | 前端常量 === Python 源码里的那个数 | 只改一侧（数字漂移） |
 * | `passwordProblem` 的边界 | 纯逻辑写错（11 位放行 / 12 位误拒） |
 * | 管理台源码里有 `minLength` 与「成功才清空」 | 修好逻辑但没接回页面（正是坑 17 的形态） |
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  fieldProblemText,
  MIN_PASSWORD_LENGTH,
  passwordProblem,
} from "../src/lib/password.ts";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../..");
const ADMIN_PAGE = readFileSync(path.resolve(HERE, "../src/app/(app)/admin/page.tsx"), "utf8");

test("★前端的口令长度下限必须等于后端 `accounts.MIN_PASSWORD_LENGTH`（防数字漂移）", () => {
  // ⚠️ 读**真的 Python 源码**取值，而不是在用例里再写一个 12：
  //    再写一个常量的话，这条用例只能证明「我抄的和我抄的一样」。
  const source = readFileSync(path.resolve(REPO_ROOT, "app/security/accounts.py"), "utf8");
  const match = source.match(/^MIN_PASSWORD_LENGTH:\s*Final\[int\]\s*=\s*(\d+)/m);
  assert.ok(match, "找不到 app/security/accounts.py 里的 MIN_PASSWORD_LENGTH（改名了？）");

  const backend = Number(match[1]);
  assert.equal(
    MIN_PASSWORD_LENGTH,
    backend,
    `前端 MIN_PASSWORD_LENGTH=${MIN_PASSWORD_LENGTH}，而后端是 ${backend}：` +
      "两边不一致时，前端要么放行一个必被后端拒的口令（管理员看到「请求参数不合规」），" +
      "要么拦住一个后端本来接受的口令 —— issue 004 就是这么来的",
  );
});

test("passwordProblem：11 位拒、12 位过（边界不许差一位）", () => {
  assert.ok(passwordProblem("passphrase-"), "11 位必须被拒（差一位都不行）");
  assert.match(String(passwordProblem("passphrase-")), /12 位/, "提示语里要写明要求几位");
  assert.equal(passwordProblem("passphrase-1"), null, "12 位必须放行");
});

test("passwordProblem：空 / 全空白 / 与标识相同都要拒", () => {
  assert.match(String(passwordProblem("")), /不能为空/);
  assert.match(String(passwordProblem("              ")), /空白/);
  // 与标识相同：大小写不敏感（后端 `lower()` 比较，口径必须一致）
  assert.match(String(passwordProblem("administrator", "administrator")), /不能与登录标识相同/);
  assert.match(String(passwordProblem("Administrator", "administrator")), /不能与登录标识相同/);
  assert.equal(
    passwordProblem("administrator", "someone-else"),
    null,
    "与标识**不同**时应当放行（不能过度收紧）",
  );
});

test("passwordProblem：报错文案不回显口令本身", () => {
  // 与后端 `validate_password_strength` 同源：凭据不该进界面/日志/截图。
  const secret = "s3cr3t-but-far-too-short";
  const message = String(passwordProblem(secret));
  assert.ok(
    !message.includes(secret),
    `报错文案里出现了口令本身：${message} —— 它会进屏幕、日志与截图`,
  );
});

test("fieldProblemText：把后端的 422 字段错误翻成「改哪里」", () => {
  // 这就是实测抓到的那个信封（issues/004 的根因 ②）
  const text = fieldProblemText({
    fields: [{ loc: ["body", "password"], msg: "String should have at least 12 characters", type: "string_too_short" }],
  });
  assert.ok(text, "422 的字段级原因必须被翻出来（否则页面只剩「请求参数不合规」）");
  assert.match(String(text), /口令/, "要指名是口令字段");
  assert.match(String(text), /12 位/, "要说清要求几位");

  // 其它字段照翻
  assert.match(String(fieldProblemText({ fields: [{ loc: ["body", "subject_id"], msg: "Input should be greater than 0" }] })), /主体 id/);
  // 认不出来的字段名：原样透传 msg（不编、不吞）
  assert.match(String(fieldProblemText({ fields: [{ loc: ["body", "weird"], msg: "some error" }] })), /some error/);
  // 没有字段信息时返回 null（由调用方走兜底文案）
  assert.equal(fieldProblemText({}), null);
  assert.equal(fieldProblemText(undefined), null);
});

test("★管理台：口令框必须带 minLength，且规则要写在界面上", () => {
  // 判据是「**口令输入框**带了这个属性」，不是「文件里出现过 minLength」——
  // 后者会被别处的写法骗过（坑 17 的教训：判据要连在真正的那一处）。
  // 窗口取得足够大：那个 `<input>` 与它的 `minLength` 之间隔着一整段解释性注释。
  const at = ADMIN_PAGE.indexOf('aria-label="初始口令"');
  assert.ok(at >= 0, "管理台里找不到「初始口令」输入框（aria-label 改了吗？）");
  const passwordInput = ADMIN_PAGE.slice(Math.max(0, at - 700), at + 700);
  assert.match(
    passwordInput,
    /minLength=\{MIN_PASSWORD_LENGTH\}/,
    "口令框没有 minLength：规则在填的时候看不见，管理员只能靠被后端拒来发现" +
      "（issue 004 的原始故障 —— 实测修复前 minLength=-1）",
  );
  // 文案判据：界面上必须写出口令长度要求。
  // ⚠️ 不要求它和 `minLength={...}` 挨在一起：那句话是 JSX 文案，
  //    中间会夹 `<span className="mono">{MIN_PASSWORD_LENGTH}</span>` 这类标记。
  //    判据是「有『至少…MIN_PASSWORD_LENGTH…位』这件事」，不是某个精确的字符序列。
  assert.match(
    ADMIN_PAGE,
    /至少[\s\S]{0,80}?MIN_PASSWORD_LENGTH[\s\S]{0,40}?位/,
    "界面上没有写出口令长度规则：规则不可见就等于没有规则" +
      "（issue 004 的原始故障 —— 管理员撞了 422 才知道有长度要求）",
  );
});

test("★管理台：口令策略必须在**发请求之前**判一次（不能只靠后端 422）", () => {
  const createBody = ADMIN_PAGE.slice(
    ADMIN_PAGE.indexOf("const create = async"),
    ADMIN_PAGE.indexOf("const resetPassword"),
  );
  assert.match(createBody, /passwordProblem\s*\(/, "建号路径没有调 passwordProblem");
  // 顺序也要对：校验必须在 run/postAccount 之前
  assert.ok(
    createBody.indexOf("passwordProblem") < createBody.indexOf("postAccount"),
    "口令校验排在请求之后了：那等于没修（请求已经发出去、后端已经拒了）",
  );

  const resetBody = ADMIN_PAGE.slice(
    ADMIN_PAGE.indexOf("const resetPassword"),
    ADMIN_PAGE.indexOf("return (", ADMIN_PAGE.indexOf("const resetPassword")),
  );
  assert.match(
    resetBody,
    /passwordProblem\s*\(/,
    "重置口令没有走同一套判据：会出现「开号时拦住、重置时放回去」",
  );
});

test("★管理台：失败时**不许**清空表单（否则管理员要全部重填再失败一次）", () => {
  const createBody = ADMIN_PAGE.slice(
    ADMIN_PAGE.indexOf("const create = async"),
    ADMIN_PAGE.indexOf("const resetPassword"),
  );
  // 清空必须被一个成功判据护住
  assert.match(
    createBody,
    /if\s*\(\s*ok\s*\)\s*setDraft\(/,
    "`setDraft` 没有被「成功」护住：失败时草稿会被清掉，" +
      "「改一个字再试」变成「全部重填再失败」（issue 004 的根因 ③）",
  );
  // 而且 run 必须把结果交出来（否则上面的 ok 恒为 undefined）
  const runBody = ADMIN_PAGE.slice(ADMIN_PAGE.indexOf("const run = async"), ADMIN_PAGE.indexOf("const create = async"));
  assert.match(runBody, /Promise<boolean>/, "run() 没有把成功/失败交回调用方");
  assert.match(runBody, /return true;/, "成功路径没有 return true");
  assert.match(runBody, /return false;/, "失败路径没有 return false");
});

test("★管理台：成功提示必须设在 `load()` **之后**（否则被 load 自己清掉）", () => {
  // 实测到的第二个缺陷：`load()` 开头 `setNotice(null)`，于是「先设成功提示、再 load」
  // 会让成功提示被自己清掉 —— 开号成功时页面一片空白，
  // 「成功」与「什么都没发生」在界面上长得一样。
  const runBody = ADMIN_PAGE.slice(ADMIN_PAGE.indexOf("const run = async"), ADMIN_PAGE.indexOf("const create = async"));
  const loadCall = runBody.indexOf("await load()");
  const okNotice = runBody.indexOf('kind: "ok"');
  assert.ok(loadCall >= 0, "run() 没有刷新列表");
  assert.ok(okNotice >= 0, "run() 没有成功提示");
  assert.ok(
    loadCall < okNotice,
    "成功提示设在 load() 之前了：`load()` 开头的 setNotice(null) 会把它清掉，" +
      "管理员开号成功却看不到任何确认（实测）",
  );
});

test("★管理台：422 必须走字段级文案分支（而不是只显示「请求参数不合规」）", () => {
  const describeBody = ADMIN_PAGE.slice(ADMIN_PAGE.indexOf("function describe"));
  assert.match(describeBody, /status === 422/, "describe() 没有区分 422");
  assert.match(
    describeBody,
    /fieldProblemText\s*\(/,
    "422 分支没有用 fieldProblemText：后端已经写清了是哪个字段、为什么，" +
      "丢掉它等于让管理员猜（issue 004 的根因 ②）",
  );
});
