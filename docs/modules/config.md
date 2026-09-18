# config — 多环境配置与密钥管理

## 1. 职责

集中管理所有运行配置与密钥，让同一份代码能无改动地在「本机开发 / Linux 生产」之间切换。
只负责**读取和校验**配置，不负责业务逻辑。
同时从第 1 步就定好「日志规范」（结构化 / 级别 / 脱敏），后续所有模块复用，避免每加一个模块回头补。

## 2. 对外接口

```python
from app.config import get_settings          # 依赖注入用（FastAPI Depends）
from app.config import Settings              # Pydantic Settings 类

settings = get_settings()
settings.database_url      # 数据库连接串
settings.redis_url         # Redis 连接串
settings.llm_primary       # 主模型名
settings.llm_api_key       # 从环境变量读，永不硬编码
settings.dump_for_logging()  # 要打印/上报配置时用它（已掩码），别用 model_dump()
```

## 3. 依赖

- `pydantic-settings`（`BaseSettings`，从 `.env` / 环境变量加载）
- `python-dotenv`（加载 `.env`）
- 被几乎所有模块依赖（db / cache / queue / llm / observability 都读它）

## 4. 关键设计决策

1. **一份配置类 + 多环境 `.env`**：用 `ENV=dev|prod` 一个开关切换，而不是维护多份 config 文件（架构第 9 节「一致性」要求）。
2. **密钥不进 Git**：`.env` 加进 `.gitignore`，只提交 `.env.example`（占位符，无真实值）。
3. **启动即校验**：Pydantic `model_validator` 在启动时校验必填项，缺 key 直接 fail-fast，避免跑一半才发现。
4. **敏感值只读不打印**：Settings 覆写 `__repr__` **和 `__str__`**，日志/异常里不泄露 API Key。
   > ⚠️ **实践修正**：只覆写 `__repr__` 不够。pydantic 的 `BaseModel.__str__` **不会调用**自定义 `__repr__`，
   > 它自己拼字段显示，于是 `str()` / f-string / json 三条出口会绕过 `__repr__` 泄露明文。
   > 必须 `__repr__` + `__str__` 一起覆写，并用 `field_serializer` 管住 json 出口（详见第 5 节「三类出口」）。

5. **配置启动即定型（`frozen=True`）**：运行期不得改写字段。`settings.env = "prod"` 或 `settings.typo = 1` 都直接抛 `ValidationError`，堵掉「改名式越权」与手滑新增属性。注意它拦的是手滑，不是深度不可变：`model_copy(update=...)` 仍可用（那是「造一个改过的副本」的正规姿势），`model_config` 与 `__pydantic_extra__` 也仍可写。
6. **`extra="allow"`**：接收 `.env` 里本组件不认识的键（其他模块的配置项），避免「加一个无关配置项」崩掉启动。`.env` 的键不会进进程环境变量，所以放宽 extra 没有放宽配置来源。
7. **LLM 路由字段只校验形状，不校验存在性**：`llm_primary` 非空、`llm_fallback` 非空时不得等于主模型；**不**校验模型名是否被厂商认识（那等于替 litellm 维护它自己的模型表）。prod 下无降级链只告警不 fail-fast —— 降级链也可能由代码内置（见 `llm.md` 第 13 步）。

## 5. 核心技术点（要懂为什么）

- **`BaseSettings` 的加载优先级**（从高到低）：进程环境变量 > `.env` 文件 > 代码默认值。生产环境用环境变量覆盖 `.env`，正是「Linux 上走环境变量注入密钥」的依据。
- **为什么用 `lru_cache` 包 `get_settings`**：保证进程内只解析一次 `.env`，多次调用返回同一实例（单例），避免重复 IO 和配置漂移。附带好处是**收敛访问入口**——想审查「谁能碰到密钥」，只需 grep `get_secret_value`。
- **`SecretStr`**：把 API Key 声明为 `SecretStr`，打印时自动脱敏为 `**********`。内部把真值存在私有属性 `_secret_value`，`__str__` / `__repr__` 走 `_secret_display()` 返回掩码，**明文只能通过 `get_secret_value()` 显式取得**——泄露从「默认行为」变成「需要主动写一行代码的动作」。
  - 序列化同样安全：`model_dump()` 返回 `SecretStr` 对象（其 repr 已掩码），`model_dump_json()` 输出 `**********`。
  - 冷知识：`SecretStr` 未定义 `__bool__` 但定义了 `__len__`，所以 `bool(SecretStr("")) is False`。校验里仍建议显式写 `.get_secret_value()`，语义更清楚。
- **⚠️ `hide_input_in_errors=True`（必须开，否则 fail-fast 反而泄露密钥）**：`model_validator(mode="after")` 抛 `ValueError` 时，pydantic 默认把**输入 dict 回显进报错文本的 `input_value`**；而 `BaseSettings` 的输入 dict 来自环境变量，是**未经 `SecretStr` 包装的原始字符串**：

  ```
  Value error, ENV=prod 缺少必填配置: database_url [type=value_error,
  input_value={'env': 'prod', 'llm_api_key': 'sk-LEAK-CANARY-999'}, input_type=dict]
  ```

  这条路径恰是**生产启动失败**时必被日志、容器 stdout、告警平台长期留存的路径，等于一次性绕过 `SecretStr` 和 `__repr__` 两层防护。加上该配置后报错收敛为 `[type=value_error]`，不含任何输入值。
- **三类出口要分开处置**（本模块最容易踩的坑）：

  | 出口 | 例子 | 处置 |
  |---|---|---|
  | ① 显示出口 | `repr()` / `str()` / f-string | **必须掩码** |
  | ② 跨边界出口 | `model_dump_json()` | **必须掩码**（json 通常意味着进日志 / HTTP / Langfuse） |
  | ③ 功能出口 | `settings.database_url` 属性、python 模式 `model_dump()` | **必须是真值**，否则连不上库 |

  只覆写 `__repr__` 只能挡住 ① 里的一条，`str()` / f-string / json 会全部绕过。正确做法是 `__repr__` + `__str__` 一起覆写，再用 `@field_serializer(..., when_used="json")` 管住 ②；③ 保持真值，并用测试把「这是有意为之」钉住，免得后人误判为漏洞而改坏功能。

  配套的**安全出口**：`dump_for_logging()` 返回与 ② 完全同源的掩码值，是「打印/上报配置」的唯一推荐入口。`model_dump()`（python 模式）的真值用途是「拿连接串去连库」，顺手打进日志就是一次全量泄露。
  > 为什么不用代码**拦**住 `model_dump()`？试过三种（无参即拒绝 / 无参打 warning / 要求 `unsafe=True`），前两种在实测里要么把 `__repr__`（它内部就是 `self.model_dump()`）一起打死、要么天天刷噪音，第三种只是给泄密新起个名字。根本原因：**这类防护只能拦直接调用，拦不住 `d = s.model_dump()` 之后间接进日志**——真实泄露路径恰恰多半是间接的。所以选择「把安全的路修得更短」，而不是把危险的路堵一半。真正针对间接泄露的机制是日志层的**值脱敏过滤器**，属于 `logging.py`（第 6 节第 5 步）。
- **连接串密码段掩码**：`database_url` / `redis_url` 内嵌密码，整体掩码会丢掉 host/port/db 的排障价值，所以只掩码密码段：
  `postgresql://u:p@localhost:5432/db` → `postgresql://u:***@localhost:5432/db`。
  正则的密码段必须用**贪婪**的 `[^/\s]*`，否则密码里出现未转义的 `@`（如 `p@ss@word`）时只掩码前半段、把后半段漏出去。
- **守卫：名字像密钥的字段必须声明为 `SecretStr`**。整层防护的前提是「敏感字段被正确声明」——新加一个 `smtp_password: str` 就会静默裸奔。用一条测试遍历 `Settings.model_fields`，凡名字含 `key/password/secret/token/credential` 的字段，类型必须是 `SecretStr`；`langfuse_public_key` 这类本就公开的值登记进 `_PUBLIC_BY_DESIGN` 豁免名单。守卫自身也要有一条「故意造违规模型确认能抓出来」的自检用例，防止守卫空转。
- **⚠️ 同名环境变量会静默覆盖 `.env`（Windows 用户级变量的实际影响）**：优先级是「进程环境变量 > `.env`」，而 Windows 用 `setx` 或「系统属性 → 环境变量」配的是**用户级全局变量**，会被注入到每一个新进程。于是只要 `.env` 里有一项与它同名，`.env` 的值就被静默丢弃，表现是「我明明改了 `.env`，配置却没变」——没有报错、没有日志，极难排查。

  **处置方式（本项目采纳）：不清除、不迁就 OS 环境变量，而是把静默变成启动期显式告警。** `Settings` 在 after-validator 里比对「`.env` 实际提供的值」与「进程环境变量值」，只在**两边都存在且值不同**时打一条 WARNING（值相同时不打扰）：

  ```
  [WARNING] 配置冲突：进程环境变量 DATABASE_URL 正在覆盖 .env 中的同名值
  （生效值: postgresql://u:***@prod-host:5432/shopsage）。
  优先级为「环境变量 > .env」，如需以 .env 为准请改环境变量名或调整其值。
  ```

  实现要点（三处容易做错）：
  1. **告警绝不打印任何一个值**：连接串只露 host/db（排障要的正是这个），`_SENSITIVE_FIELDS` 里的密钥整体掩码 —— 否则「为了排障加的告警」本身成了新的泄露出口。
  2. **对账取 pydantic 解析后的字段名，不手写环境变量名映射**：通过 `settings_customise_sources` 拿到 `DotEnvSettingsSource` 并调用它，得到 `{'database_url': '...'}`（键是字段名，别名/前缀/大小写已由 pydantic 处理）。若改成自己拼接 `name.upper()`，就等于「用手写规则猜 pydantic 的映射」，迟早对不上。
  3. **必须用 source 而不是 `model_config["env_file"]`**：`Settings(_env_file=...)` 是按**实例**传的，pydantic 实例化后不保留该属性，`model_config` 里永远只有类默认值 `".env"`。只读 `model_config` 的话，per-instance 覆盖会整个漏检（这个 bug 是被 `test_settings_wires_collision_guard` 抓出来的）。

  告警用模块级 `logging.getLogger(__name__)`，**不加任何 handler** —— 日志输出位置交给 `app/config/logging.py`（第 6 节第 5 步）。未配置日志时 Python 的 lastResort handler 仍会把 WARNING 打到 stderr，所以这条告警在任何情况下都看得见，这正是 fail-loud 的本意。

  > 这样做的收益：**不需要为了避坑去清空用户环境变量**。开发机上已经有 `DEEPSEEK_API_KEY` / `LANGFUSE_*` 这类全局变量时，只要它们与 `.env` 冲突，启动就会明确告诉你，而不是安静地用错配置。
  >
  > 边界说明：`.env` 里放的是**项目专属**配置；真正的密钥在生产走容器运行时注入。若某台开发机的全局变量与 `.env` 长期冲突且你**不想**改任何一个值，正确做法是把该字段改名为更专属的名字（如 `LANGFUSE_PUBLIC_KEY` → 项目前缀形式），而不是容忍静默覆盖。
- **`extra="allow"`：`.env` 里的未知键不报错**。各模块会各自往 `.env` 加键（如 RAG 的 `EMBEDDING_MODEL`），本组件不认识就报错会让「加一个无关配置项」直接崩掉启动。注意 `.env` 的键**不会**进进程环境变量，所以放宽 `extra` 并没有放宽配置来源。
  > 实测：`extra="allow"` 下这些键会出现在 `s.__pydantic_extra__`（如 `{'embedding_model': 'text-embedding-3-small'}`），冲突检测因此连「其他模块的键」也能覆盖到。

## 6. 开发步骤

1. 建 `app/config/__init__.py` + `settings.py`，定义 `Settings(BaseSettings)`。
2. 字段覆盖：`ENV`、`database_url`、`redis_url`、`llm_primary`、`llm_fallback`、`llm_api_key: SecretStr`、`otel_endpoint`、`langfuse_public_key`、`log_level` 等。
3. 加 `model_validator`：`ENV=prod` 时必须存在 `llm_api_key`、`database_url`，否则 raise。
4. 覆写 `__repr__` **和 `__str__`** 隐藏敏感字段（只写 `__repr__` 会漏掉 `str()`/f-string 出口）；再给 `database_url` / `redis_url` 加 `field_serializer(when_used="json")` 掩码密码段。
5. 写 `app/config/logging.py`，定「日志规范」：结构化 JSON、统一 logger 名、敏感字段不打印（配合 `__repr__` 约定）。
6. 写 `.env.example`（全部占位符）+ 把 `.env` 加入 `.gitignore`。
7. 加**同名环境变量冲突告警**（见第 5 节最后两条要点）：`settings_customise_sources` 记录 `DotEnvSettingsSource`，after-validator 里比对 `.env` 值与进程环境变量值，冲突时 WARNING（值相同不打扰、绝不打印任何值）。
8. 写单测（见下）。

## 7. 测试用例

### 7.1 基础用例

```python
def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    s = Settings()  # ENV 默认 dev
    assert s.database_url == "postgresql://u:p@localhost/db"

def test_prod_requires_llm_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()

def test_secret_not_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert "sk-secret-123" not in repr(s)
```

### 7.2 信息隔离回归用例（每条对应第 5 节一个泄露面）

```python
@pytest.mark.parametrize(
    "render",
    [lambda s: repr(s),
     lambda s: str(s),
     lambda s: f"{s}",
     lambda s: s.model_dump_json(),
     lambda s: repr(s.model_dump(mode="json"))],
    ids=["repr", "str", "fstring", "dump_json", "dump_json_mode"],
)
def test_no_leak_through_display_and_boundary_exits(monkeypatch, render):
    """显示出口 + 跨边界出口都不得泄露密钥。
    回归：pydantic 的 BaseModel.__str__ 不调用自定义 __repr__，
    只覆写 __repr__ 时 str()/f-string/json 三条出口会全部泄露连接串密码。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-LEAK-CANARY-999")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:DB-CANARY-111@localhost:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://:REDIS-CANARY-222@localhost:6379/0")
    out = render(Settings())
    for canary in ("sk-LEAK-CANARY-999", "DB-CANARY-111", "REDIS-CANARY-222"):
        assert canary not in out, f"{canary} 从渲染出口泄露了"

def test_validation_error_does_not_leak_key(monkeypatch):
    """回归：after-validator 抛 ValueError 时，pydantic 默认把输入 dict
    回显进 input_value，导致启动失败的日志泄露 api key。"""
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("LLM_API_KEY", "sk-LEAK-CANARY-999")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "sk-LEAK-CANARY-999" not in str(exc.value)

def test_python_dump_is_intentionally_a_functional_exit(monkeypatch):
    """契约固定：python 模式 model_dump() 返回真值，属功能出口。
    真值拿不到就连不上库；这条测试钉住「有意为之」，防止被当漏洞改坏。"""
    raw = "postgresql://u:dbsecret@localhost:5432/db"
    monkeypatch.setenv("DATABASE_URL", raw)
    assert Settings().model_dump()["database_url"] == raw

@pytest.mark.parametrize(("raw", "expected"), [
    ("postgresql://u:p@localhost:5432/db", "postgresql://u:***@localhost:5432/db"),
    ("redis://:onlypass@localhost:6379/0", "redis://:***@localhost:6379/0"),
    # 密码含未转义 @：必须整段吃掉，不能只掩码前半段
    ("postgresql://user:p@ss@word@host/db", "postgresql://user:***@host/db"),
    # 无密码：不做无意义的掩码
    ("redis://localhost:6379/0", "redis://localhost:6379/0"),
])
def test_url_password_masking(raw, expected):
    from app.config.settings import _mask_url_password
    assert _mask_url_password(raw) == expected

def test_secret_named_fields_must_be_secret_str():
    """守卫：否则新加一个 smtp_password: str 就会静默裸奔。"""
    offenders = _audit_secret_named_fields(Settings)
    assert not offenders, f"这些字段名字像密钥但不是 SecretStr: {offenders}"

def test_audit_guard_actually_catches_violations():
    """守卫有效性自检：故意造违规模型，确认抓得出来（防止守卫空转）。"""
    class _BadConfig(BaseModel):
        smtp_password: str = ""
        api_token: str = ""
        langfuse_public_key: str = ""   # 公开值，应被豁免
    assert set(dict(_audit_secret_named_fields(_BadConfig))) == {"smtp_password", "api_token"}
```

> 攻击/异常用例的思路：**不要只测「正常情况脱敏了」，要逐条测「每个泄露出口」**。
> 上面第一组用参数化把 5 条出口逐一钉死，用 canary 值（`sk-LEAK-CANARY-999`）而非真实格式，
> 保证断言是「值完全不出现」而不是「看起来像掩码」。

## 8. 验收标准

- [x] `.env` 不入 Git（`git status` 无 `.env`），`.env.example` 已提交。
- [x] `ENV=prod` 缺失 `LLM_API_KEY` 时启动直接失败（fail-fast）。
- [x] 第 7.1 节 3 个基础测试全绿。
- [x] 第 7.2 节信息隔离回归全绿：fail-fast 报错不含密钥、显示出口 + json 出口全部掩码、连接串密码段掩码、敏感字段命名守卫生效。
- [x] 同名环境变量静默覆盖 `.env` 时启动告警，且告警本身不含任何密钥或连接串密码明文。
- [x] 告警**不要求**清理或迁就 OS 环境变量：无需任何环境变量操作即可正常开发。
- [x] `frozen=True`：运行期字段赋值与静默新增属性被拒，`model_copy(update=...)` 仍可用。
- [x] 提供 `dump_for_logging()` 作为「打印配置」的唯一推荐出口，且与 json 出口掩码规则一致。
- [x] LLM 路由字段形状校验：`llm_primary` 非空、`llm_fallback` 非空时不得等于主模型；prod 无降级链时告警而非 fail-fast。
- [ ] 证据链接：`PR#__`

**证据（本地验证，2026-09-18）**

| 项 | 内容 |
|---|---|
| 被测代码 | `app/config/settings.py`、`app/config/__init__.py` |
| 测试代码 | `tests/test_config.py`（26 条用例） |
| 复跑命令 | `pytest -q`（`pyproject.toml` 已配 `pythonpath=["."]`、`testpaths=["tests"]`） |
| 结果 | 26 passed / 0 failed |

> 说明：本机环境未安装 pytest（无网络），上表的 26 条用例是**用等价 runner 逐条执行通过**的；
> 开发机或 CI 装上 pytest 后 `pytest -q` 可直接复跑。

**证据（同名环境变量冲突告警，2026-09-18 追加）**

| 项 | 内容 |
|---|---|
| 被测代码 | `app/config/settings.py`（`_read_dotenv_values` / `_dotenv_field_values` / `_warn_on_env_collisions` / `Settings.settings_customise_sources`） |
| 测试代码 | `tests/test_config.py`（新增 11 条：3 条冲突告警 + 1 条端到端接线 + 1 条无 .env 时不打扰 + 3 条 .env 原文读取 + 1 条 extra 不报错 + 2 条已并入上面的参数化） |
| 复跑命令 | `python -m pytest -q` |
| 结果 | 37 passed / 0 failed（26 条原有 + 11 条新增） |
| 独立验证 | 另有一次性脚本逐项验证 helper（值相同/不同、大小写、prefix、非字符串字段、密钥掩码、候选列表顺序）+ `Settings()` 端到端，26 项全通过 |

> 环境说明：本次验证在「Windows + 受限文件沙箱」下进行，pytest 的临时根
> （`%TEMP%\pytest-of-<user>`）会被首次运行以 `0o700` 创建，而该沙箱禁止 `chmod`，
> 于是**同一 shell 里第二次运行**会因无权进入该目录而报 `PermissionError [WinError 5]`
> （表现为 6 条用 `tmp_path` 的用例集体 error）。**这是环境限制，不是代码问题**：
> 全新 shell 下 `python -m pytest -q` 稳定 37 passed / 退出码 0。普通开发机与 CI 无此问题。

**证据（三项加固，2026-09-18 追加）**

三项内容：`dump_for_logging()`（议题 2）、`frozen=True`（议题 5）、LLM 路由字段形状校验（议题 6）。

| 项 | 内容 |
|---|---|
| 被测代码 | `app/config/settings.py`（`model_config.frozen` / `dump_for_logging` / `_validate_llm_routing` / prod 无降级链告警） |
| 测试代码 | `tests/test_config.py` 新增 17 条：frozen 3 条、`dump_for_logging` 4 条、llm 路由 7 条（含 `llm_primary` 3 个空值参数化 + 1 条「规范化落到所有出口」）、prod 降级链告警 2 条、`extra="allow"` 1 条 |
| 用例总数 | 54 条（37 条此前 + 17 条新增） |
| 复跑命令 | `python -m pytest -q` |
| 独立验证 | 一次性脚本逐项验证三项行为，26 项全通过（frozen 的 4 种行为、`dump_for_logging` 的 9 项掩码/一致性、llm 路由 9 项、prod 告警 4 项） |
| 定向复跑 | `pytest -k "frozen or dump_for_logging or llm_ or prod_with" -W error::DeprecationWarning` → 19 passed / 退出码 0（把 pydantic 的 DeprecationWarning 升级为失败也能过，确认 frozen 下的规范化赋值不是靠已弃用行为） |

三项的关键实测结论（都写成了回归用例）：

- `frozen=True` 抛的是 **`ValidationError`**（不是 `AttributeError`），且不阻断 `model_copy(update=...)`。
- 无参 `model_dump()` 仍是真值（功能出口未被削弱）；`dump_for_logging()` 与 `model_dump_json()` 输出完全相等，掩码规则不可能漂移。
- LLM 路由**刻意不校验模型名是否存在** —— 那是替 litellm 维护它自己的模型表；任何非空且互不相同的名字都放行。
- 回归：prod 无降级链的告警文本里曾把 `99.9%` 写成字面 `99.9%%`（该日志没有 `%`-插值参数，logging 不做 `%` 处理），已修并用测试钉住。

### 尚未完成

- [ ] 第 6 节第 5 步 `app/config/logging.py`：日志规范（结构化 JSON、统一 logger 名、敏感字段不打印）。
- [ ] 第 6 节第 2 步的 `log_level` 字段：未加进 `Settings`（依赖上面的 logging 模块，故一并待办）。

> 完成度：第 1、3、4、6、7 步已完成；第 2 步除 `log_level` 外已完成；第 5 步未开始。
