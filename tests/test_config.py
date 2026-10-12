"""config 模块单测，对应 docs/modules/config.md 第 7 节。"""

import pytest
from pydantic import BaseModel, SecretStr, ValidationError
import json

from app.config import Settings
from app.config import get_settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    s = Settings()  # ENV 默认 dev
    assert s.database_url == "postgresql://u:p@localhost/db"


def test_prod_requires_llm_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(ValidationError):
        print(json.dumps(get_settings().dump_for_logging(), ensure_ascii=False, indent=2))


def test_secret_not_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert "sk-secret-123" not in repr(s)


def test_prod_ok_when_required_present(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    # 显式给 fallback 的独立密钥：仓库 .env 里若配了 LLM_FALLBACK，prod 下缺这把钥匙会拒绝启动
    # （见 _validate_prod_requirements 与 runbook §1.2.1）。这条断言的就是「全都配好 → 能启动」。
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "sk-fallback-secret-456")
    s = Settings()
    assert s.env == "prod"
    assert s.database_url == "postgresql://u:p@localhost/db"


def test_get_settings_is_singleton(monkeypatch):
    from app.config import get_settings

    a = get_settings()
    b = get_settings()
    assert a is b


# --- 信息隔离：逐条堵死泄露出口 ---


def test_validation_error_does_not_leak_key(monkeypatch):
    """fail-fast 报错文本里不能出现密钥原文。

    回归用例：after-validator 抛 ValueError 时，pydantic 默认会把输入 dict
    回显进 input_value，导致启动失败的日志泄露 api key。
    """
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("LLM_API_KEY", "sk-LEAK-CANARY-999")
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "sk-LEAK-CANARY-999" not in str(exc.value)


@pytest.mark.parametrize(
    "render",
    [
        lambda s: repr(s),
        lambda s: str(s),
        lambda s: f"{s}",
        lambda s: s.model_dump_json(),
        lambda s: repr(s.model_dump(mode="json")),
    ],
    ids=["repr", "str", "fstring", "dump_json", "dump_json_mode"],
)
def test_no_leak_through_display_and_boundary_exits(monkeypatch, render):
    """显示出口 + 跨边界序列化出口，都不得泄露任何密钥。

    回归用例：pydantic 的 BaseModel.__str__ 不调用自定义 __repr__，
    最初只覆写 __repr__ 时，str()/f-string/json 三条出口全部泄露连接串密码。
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-LEAK-CANARY-999")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:DB-CANARY-111@localhost:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://:REDIS-CANARY-222@localhost:6379/0")
    out = render(Settings())
    for canary in ("sk-LEAK-CANARY-999", "DB-CANARY-111", "REDIS-CANARY-222"):
        assert canary not in out, f"{canary} 从渲染出口泄露了"


def test_python_dump_is_intentionally_a_functional_exit(monkeypatch):
    """契约固定：python 模式 model_dump() 返回真值，属功能出口，禁止直接打日志。

    真值必须拿得到，否则连不上数据库；这条测试把「有意为之」钉住，
    防止以后有人误判为漏洞而把功能改坏。
    """
    raw = "postgresql://u:dbsecret@localhost:5432/db"
    monkeypatch.setenv("DATABASE_URL", raw)
    s = Settings()
    assert s.model_dump()["database_url"] == raw


def test_secret_requires_explicit_access(monkeypatch):
    """默认拿到的是 SecretStr 对象，取值必须显式调用 get_secret_value()。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert isinstance(s.llm_api_key, SecretStr)
    assert s.llm_api_key.get_secret_value() == "sk-secret-123"


# --- 连接串密码段掩码（database_url / redis_url） ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@localhost:5432/db", "postgresql://u:***@localhost:5432/db"),
        (
            "postgresql+asyncpg://user:s3cr3t@db.internal:5432/x",
            "postgresql+asyncpg://user:***@db.internal:5432/x",
        ),
        ("redis://:onlypass@localhost:6379/0", "redis://:***@localhost:6379/0"),
        ("postgresql://user:p%40ss@host/db", "postgresql://user:***@host/db"),
        ("mysql://root:pw@[::1]:3306/db", "mysql://root:***@[::1]:3306/db"),
        # 密码含未转义的 @：必须整段吃掉，不能只掩码前半段而漏出后半段
        ("postgresql://user:p@ss@word@host/db", "postgresql://user:***@host/db"),
        # 无密码 / 无凭据 / 空串：不做无意义的掩码
        ("postgresql://u@localhost:5432/db", "postgresql://u@localhost:5432/db"),
        ("redis://localhost:6379/0", "redis://localhost:6379/0"),
        ("", ""),
    ],
)
def test_url_password_masking(raw, expected):
    from app.config.settings import _mask_url_password

    assert _mask_url_password(raw) == expected


def test_repr_masks_url_passwords_but_keeps_debug_info(monkeypatch):
    """连接串密码要掩码，但 host/port/db 必须保留，否则排障时看不出连的哪。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:dbsecret@localhost:5432/shopsage")
    monkeypatch.setenv("REDIS_URL", "redis://:redissecret@localhost:6379/0")
    r = repr(Settings())
    assert "dbsecret" not in r
    assert "redissecret" not in r
    assert "postgresql://u:***@localhost:5432/shopsage" in r
    assert "redis://:***@localhost:6379/0" in r


def test_masking_is_display_only(monkeypatch):
    """脱敏只作用于显示：字段真值必须原样保留，否则生产就连不上库了。"""
    raw = "postgresql://u:dbsecret@localhost:5432/db"
    monkeypatch.setenv("DATABASE_URL", raw)
    s = Settings()
    assert s.database_url == raw
    assert "dbsecret" in s.database_url


# --- 守卫：名字像密钥的字段必须声明为 SecretStr ---

# ⚠️ 判据是「**以下划线分隔的最后一段**像密钥」，不是「名字里含 key」——
# 第 10 步（rag）加进来的 `rag_keyword_min_ratio`（关键词腿的覆盖率阈值，一个 float 比例）
# 会被「含 key」的判据误判成密钥，而它显然不是（同类还有 `max_tokens` 这种参数名）。
# 收紧成后缀判据之后，`smtp_password` / `api_token` / `llm_api_key` 仍然会被抓到，
# 而「参数名里恰好有 key 这个词」的字段不再误报。
_SECRET_NAME_HINTS = ("key", "password", "passwd", "secret", "token", "credential")
# 本就是公开值、允许不是 SecretStr 的字段（如 Langfuse 的 public key）
_PUBLIC_BY_DESIGN = frozenset({"langfuse_public_key"})


def _looks_like_secret_name(name: str) -> bool:
    """字段名是否「看起来像密钥」：末段是 key/password/token/secret/credential。

    例：`llm_api_key` → True（末段 key）；`rag_rerank_api_key` → True；
    `rag_keyword_min_ratio` → False（末段是 ratio）；`max_tokens` → False。
    """
    tail = name.lower().split("_")[-1]
    return tail in _SECRET_NAME_HINTS


def _audit_secret_named_fields(model) -> list[tuple[str, object]]:
    """返回「名字像密钥、类型却不是 SecretStr」的字段清单。"""
    offenders = []
    for name, field in model.model_fields.items():
        if name in _PUBLIC_BY_DESIGN:
            continue
        if not _looks_like_secret_name(name):
            continue
        if field.annotation is not SecretStr:
            offenders.append((name, field.annotation))
    return offenders


def test_secret_named_fields_must_be_secret_str():
    """守卫：否则新加一个 smtp_password: str 就会静默裸奔。"""
    offenders = _audit_secret_named_fields(Settings)
    assert not offenders, (
        f"这些字段名字像密钥但不是 SecretStr: {offenders}。"
        "要么改成 SecretStr，要么登记进 _PUBLIC_BY_DESIGN 说明它确实是公开值。"
    )


def test_audit_guard_actually_catches_violations():
    """守卫有效性自检：故意造违规模型，确认抓得出来（防止守卫空转）。"""

    class _BadConfig(BaseModel):
        smtp_password: str = ""
        api_token: str = ""
        langfuse_public_key: str = ""  # 公开值，应被豁免

    caught = dict(_audit_secret_named_fields(_BadConfig))
    assert set(caught) == {"smtp_password", "api_token"}


# --- 冲突告警：OS 环境变量静默覆盖 .env ---
#
# 背景：优先级是「环境变量 > .env」，所以 Windows 用户级环境变量（setx / 「系统属性
# → 环境变量」里配的）会静默盖掉项目 .env 的同名项，表现为「我明明改了 .env，
# 配置却没变」。这里把静默变成显式告警，从而**不必清除或迁就 OS 环境变量**。
#
# 本组用例用 caplog 断言（logger 的 propagate 为 True，caplog 能正常收到）。


def test_env_collision_emits_warning_and_masks(caplog):
    """同名环境变量与 .env 值不同 → 告警，且两个值都不出现在告警里。

    注意 `dotenv` 参数用**字段名**作键（DotEnvSettingsSource 的解析结果），
    不是环境变量名。
    """
    from app.config.settings import _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={
                "database_url": "postgresql://u:OS-CANARY-111@prod-host:5432/db",
                "llm_api_key": SecretStr("sk-OS-CANARY-222"),
            },
            dotenv={
                "database_url": "postgresql://u:FILE-CANARY-333@localhost:5432/db",
                "llm_api_key": "sk-FILE-CANARY-444",
            },
            env={
                "DATABASE_URL": "postgresql://u:OS-CANARY-111@prod-host:5432/db",
                "LLM_API_KEY": "sk-OS-CANARY-222",
            },
            env_prefix="",
            case_sensitive=False,
        )

    joined = " | ".join(r.getMessage() for r in caplog.records)
    assert "DATABASE_URL" in joined and "LLM_API_KEY" in joined
    for canary in (
        "OS-CANARY-111",
        "FILE-CANARY-333",
        "sk-OS-CANARY-222",
        "sk-FILE-CANARY-444",
    ):
        assert canary not in joined, f"{canary} 从冲突告警里泄露了"
    # 连接串保留 host/db，方便判断「生效的到底连哪个库」
    assert "prod-host:5432/db" in joined


def test_env_collision_silent_when_values_equal(caplog):
    """值相同 → 不告警：否则开发机上同名同值会天天刷噪音。"""
    from app.config.settings import _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={"redis_url": "redis://localhost:6379/0"},
            dotenv={"redis_url": "redis://localhost:6379/0"},
            env={"REDIS_URL": "redis://localhost:6379/0"},
            env_prefix="",
            case_sensitive=False,
        )
    assert not caplog.records


def test_env_collision_silent_when_only_one_side_present(caplog):
    """只有一边有 → 不告警（不存在覆盖关系）。"""
    from app.config.settings import _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={"otel_endpoint": "http://localhost:4317"},
            dotenv={"otel_endpoint": "http://localhost:4317"},
            env={},
            env_prefix="",
            case_sensitive=False,
        )
        _warn_on_env_collisions(
            field_values={"langfuse_public_key": "pk-1"},
            dotenv={},
            env={"LANGFUSE_PUBLIC_KEY": "pk-1"},
            env_prefix="",
            case_sensitive=False,
        )
    assert not caplog.records


def test_env_collision_empty_dotenv_value_does_not_crash(caplog):
    """`.env` 里该项为空 → 告警文案必须能生成（回归：NameError: ENV_FILE_NAME）。

    这条分支与「两处值不同」是**两条独立文案**，只在 `.env` 里留空时才会走到。
    曾经的 bug 正是这一支引用了未定义的名字，于是「用户为了不覆盖而把 .env 留空」
    ——也就是最该被正确引导的那种配置——会让 `Settings()` 直接崩在 import 期，
    连 `--dry-run` 都跑不起来。断言点：不抛异常、文案里点明是哪个文件、不泄露值。
    """
    from app.config.settings import ENV_FILE_NAME, _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={"database_url": "postgresql://u:ospass@prod-host:5432/db"},
            dotenv={"database_url": ""},
            env={"DATABASE_URL": "postgresql://u:ospass@prod-host:5432/db"},
            env_prefix="",
            case_sensitive=False,
        )

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert ENV_FILE_NAME in msg, "文案要指明去哪个文件里改"
    assert "空的" in msg, "空值分支应说明「是环境变量在提供它，不是覆盖」"
    assert "ospass" not in msg, "告警泄露了密码明文"


def test_env_file_name_constant_matches_model_config():
    """告警文案里的文件名与真正读取的文件名必须是同一个来源，不能各写一份。"""
    from app.config.settings import ENV_FILE_NAME

    assert Settings.model_config["env_file"] == ENV_FILE_NAME


def test_env_collision_respects_prefix_and_case_insensitive(caplog):
    """env_prefix 参与对账；名字大小写不同仍是同一个变量（Windows 语义）。"""
    from app.config.settings import _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={"llm_api_key": SecretStr("sk-os")},
            dotenv={"llm_api_key": "sk-file"},
            env={"shopsage_llm_api_key": "sk-os"},
            env_prefix="SHOPSAGE_",
            case_sensitive=False,
        )
    assert len(caplog.records) == 1
    assert "SHOPSAGE_LLM_API_KEY" in caplog.records[0].getMessage()
    assert "sk-os" not in caplog.records[0].getMessage()


def test_env_collision_skips_non_string_fields(caplog):
    """非字符串字段不与 .env 原文对账，且绝不能抛异常。"""
    from app.config.settings import _warn_on_env_collisions

    with caplog.at_level("WARNING", logger="app.config.settings"):
        _warn_on_env_collisions(
            field_values={"env": "dev", "some_list": [1, 2], "some_secret": SecretStr("")},
            dotenv={"some_list": "x", "some_secret": "x"},
            env={"SOME_LIST": "x", "SOME_SECRET": "x"},
            env_prefix="",
            case_sensitive=False,
        )
    assert not caplog.records


def test_settings_wires_collision_guard(monkeypatch, tmp_path, caplog):
    """端到端接线：Settings() 真的会调冲突检测，且优先级仍是「环境变量 > .env」。

    用 `_env_file=` 指向临时 .env，因而不依赖仓库里是否存在 .env。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://u:filepass@localhost:5432/db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:ospass@prod-host:5432/db")
    monkeypatch.setenv("LLM_PRIMARY", "deepseek")

    with caplog.at_level("WARNING", logger="app.config.settings"):
        s = Settings(_env_file=env_file)

    joined = " | ".join(r.getMessage() for r in caplog.records)
    assert "DATABASE_URL" in joined, "冲突检测没有被 Settings 调用"
    assert "filepass" not in joined and "ospass" not in joined, "告警泄露了密码明文"
    assert "prod-host:5432/db" in joined, "告警应保留 host/db 便于排障"
    # 优先级契约：环境变量赢
    assert s.database_url == "postgresql://u:ospass@prod-host:5432/db"


def test_no_collision_warning_when_env_file_absent(monkeypatch, tmp_path, caplog):
    """没有 .env 可读时零输出、零影响（不制造噪音、不影响取值）。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("LLM_PRIMARY", "deepseek")
    with caplog.at_level("WARNING", logger="app.config.settings"):
        s = Settings(_env_file=tmp_path / "definitely-missing.env")

    assert not caplog.records
    assert s.database_url == "postgresql://u:p@localhost:5432/db"


# --- .env 原文读取（冲突检测的对账基础） ---


def test_read_dotenv_values_reads_file(tmp_path):
    from app.config.settings import _read_dotenv_values

    p = tmp_path / ".env"
    p.write_text("A=1\nB=two\n# 注释\nC=\n", encoding="utf-8")
    assert _read_dotenv_values(str(p), "utf-8") == {"A": "1", "B": "two", "C": ""}


def test_read_dotenv_values_returns_none_when_unavailable(tmp_path):
    """不可读 / None / 异常类型一律返回 None —— 检测是尽力而为，不得阻断启动。"""
    from app.config.settings import _read_dotenv_values

    assert _read_dotenv_values(str(tmp_path / "missing.env"), "utf-8") is None
    assert _read_dotenv_values(None, "utf-8") is None
    assert _read_dotenv_values(123, "utf-8") is None
    assert _read_dotenv_values(object(), "utf-8") is None


def test_read_dotenv_values_candidate_list_order(tmp_path):
    """候选列表：后读到的覆盖先读到的，与 pydantic-settings 的顺序语义一致。"""
    from app.config.settings import _read_dotenv_values

    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("A=1\nB=first\n", encoding="utf-8")
    second.write_text("B=second\n", encoding="utf-8")

    assert _read_dotenv_values([str(first), str(second)], "utf-8") == {"A": "1", "B": "second"}


def test_extra_keys_are_accepted_not_rejected(tmp_path,monkeypatch):
    """extra="allow"：.env 里**不属于本组件**的键不得导致启动失败。

    这是刻意选择：各模块会各自往 .env 加键，本组件不认识就报错，会让「加一个
    无关配置项」直接崩掉启动。注意 `.env` 的键不会进进程环境变量，所以放宽 extra
    并没有放宽配置来源。

    ⚠️ 这里用的键必须是一个**真实存在、且当前未被声明**的「别人的键」。本用例原先拿
    `EMBEDDING_MODEL` 当例子，后来换 `LANGFUSE_HOST` / `RAG_CHUNK_SIZE` —— 它们**都已经在后续
    步骤里被正式声明**了（`langfuse_host` 是字段名，真正的字段是 `rag_chunk_max_chars`，
    `rag_chunk_size` 压根不存在）。声明之后它们不再进 `__pydantic_extra__`，断言退化成
    `None == '...'`。这不是回归，而是这条用例的**例子过期了**：凡是「拿某个键举例」的用例，
    都要在它被正式声明时一起改。所以这里改用 `API_*`（`app/api/config.py` 的键，本组件不声明，
    且该模块正是靠 `extra="allow"` + `os.environ` 两条路读它们）。

    另外：断言的是「经 extra 通道拿到的是 .env 里的值」。若同名进程环境变量存在，
    优先级 env > .env 会让断言拿不到 .env 的值（Windows 用户级变量的老问题，本机实测
    `LANGFUSE_HOST` 就是被进程环境变量盖掉的）。所以先把这两个键从进程环境里摘掉，
    让用例只测「.env 的额外键不被拒」这一件事。
    """
    p = tmp_path / ".env"
    p.write_text("API_PREFIX=/v2\nAPI_DEBUG_PAYLOAD=1\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PRIMARY", "deepseek")
    monkeypatch.delenv("API_PREFIX", raising=False)
    monkeypatch.delenv("API_DEBUG_PAYLOAD", raising=False)
    s = Settings(_env_file=p)
    extra = s.__pydantic_extra__ or {}
    assert extra.get("api_prefix") == "/v2"
    assert extra.get("api_debug_payload") == "1"


# --- frozen：配置启动即定型，运行期不得改写 ---


def test_frozen_rejects_field_assignment():
    """防「改名式越权」：settings.llm_api_key = ... 必须失败。

    注意抛的是 ValidationError（pydantic 语义），不是 AttributeError。
    """
    s = Settings()
    with pytest.raises(ValidationError):
        s.env = "prod"
    with pytest.raises(ValidationError):
        s.llm_api_key = SecretStr("sk-injected")


def test_frozen_rejects_silent_extra_attribute():
    """防手滑：frozen 下新建属性也失败，避免 `settings.typo = 1` 静默生效。"""
    s = Settings()
    with pytest.raises(ValidationError):
        s.typo_field = 1


def test_frozen_still_allows_model_copy():
    """冻结不阻断「造一个改过的副本」这条正规姿势。"""
    s = Settings()
    assert s.model_copy() is not s
    updated = s.model_copy(update={"env": "prod"})
    assert updated.env == "prod"
    assert s.env == "dev", "原实例不得被副本改动影响"


# --- dump_for_logging：打日志用的安全出口 ---


def test_dump_for_logging_masks_everything_sensitive():
    """「打印配置」只该走这里；四类金丝雀（两个连接串密码 + 两个密钥）都不出现。"""
    s = Settings(
        database_url="postgresql://u:DB-CANARY-111@prod-host:5432/db",
        redis_url="redis://:REDIS-CANARY-222@localhost:6379/0",
        llm_api_key=SecretStr("sk-LEAK-CANARY-999"),
        langfuse_secret_key=SecretStr("lf-CANARY-444"),
    )
    dumped = s.dump_for_logging()
    joined = repr(dumped)
    for canary in ("DB-CANARY-111", "REDIS-CANARY-222", "sk-LEAK-CANARY-999", "lf-CANARY-444"):
        assert canary not in joined, f"{canary} 从 dump_for_logging 泄露了"
    # 保留 host/db：排障要的正是「连的哪个库」
    assert dumped["database_url"] == "postgresql://u:***@prod-host:5432/db"
    assert dumped["llm_api_key"] == "**********"


def test_dump_for_logging_matches_json_exit():
    """掩码规则不许漂移：两条安全出口必须是同一套规则（实现上共用）。"""
    s = Settings(
        database_url="postgresql://u:p@h:5432/db",
        llm_api_key=SecretStr("sk-x"),
    )
    assert s.dump_for_logging() == s.model_dump(mode="json")


def test_dump_for_logging_returns_detached_copy():
    """返回可变副本：调用方随手改不会污染冻结的实例。"""
    s = Settings()
    dumped = s.dump_for_logging()
    dumped["env"] = "tampered"
    assert s.env == "dev"


def test_functional_exit_unaffected_by_safe_helper():
    """加了安全出口不等于把功能出口关掉：python 模式 model_dump() 仍是真值。"""
    raw = "postgresql://u:dbsecret@localhost:5432/db"
    s = Settings(database_url=raw)
    assert s.model_dump()["database_url"] == raw
    assert "dbsecret" not in repr(s.dump_for_logging())


# --- llm 路由字段的形状校验 ---


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_llm_primary_must_not_be_blank(blank):
    """主模型为空 = 调用必然失败，且发生在请求路径上 → 启动即拦。"""
    with pytest.raises(ValidationError):
        Settings(llm_primary=blank)


def test_llm_fallback_must_differ_from_primary():
    """备模型等于主模型 → 「降级」变成重复打同一个模型，静默丧失可用性收益。

    ⚠️ 这里**必须显式传 llm_primary**：不传就会读仓库根 `.env` 的 `LLM_PRIMARY`，
    于是这条用例会随环境变化而失效（实测踩到：把 .env 的 primary 改成 `deepseek/deepseek-flash`
    后，`llm_fallback="deepseek"` 不再相等，用例变成「DID NOT RAISE」）。
    凡是断言「两个字段之间的关系」的用例，两个字段都要显式给。
    """
    with pytest.raises(ValidationError):
        Settings(llm_primary="deepseek", llm_fallback="deepseek")
    with pytest.raises(ValidationError):
        Settings(llm_primary="deepseek", llm_fallback="  deepseek  ")  # 去空格后仍相等


def test_llm_fallback_empty_is_legal():
    """空 fallback 是合法语义「不启用降级」，不能在启动时报错。"""
    assert Settings(llm_fallback="").llm_fallback == ""


def test_llm_model_names_are_stripped():
    """首尾空白会被规范化：避免 " gpt-4o " 这种模型名到调用时才炸。"""
    s = Settings(llm_primary="  gpt-4o  ", llm_fallback="  gpt-4o-mini  ")
    assert s.llm_primary == "gpt-4o"
    assert s.llm_fallback == "gpt-4o-mini"


def test_llm_normalization_applies_to_every_exit():
    """规范化必须落到 __dict__，否则会出现「属性是 A、JSON 出口是 B」的隐性不一致。

    回归：frozen 下用 object.__setattr__ 在 after-validator 里改值，
    需要确认序列化器读到的是改后的值（而不是 after-validator 之前的快照）。
    """
    s = Settings(llm_primary="  gpt-4o  ")
    assert s.__dict__["llm_primary"] == "gpt-4o"
    assert s.model_dump()["llm_primary"] == "gpt-4o"
    assert '"llm_primary":"gpt-4o"' in s.model_dump_json()
    assert s.dump_for_logging()["llm_primary"] == "gpt-4o"
    assert "'llm_primary': 'gpt-4o'" in repr(s)


def test_llm_validation_does_not_check_model_existence():
    """刻意不校验模型名是否被厂商认识 —— 那是替 litellm 维护它的模型表。

    任何非空且互不相同的名字都必须放行（包括尚不存在的名字）。
    """
    s = Settings(llm_primary="totally-made-up-model-xyz", llm_fallback="another-made-up")
    assert s.llm_primary == "totally-made-up-model-xyz"


def test_prod_without_fallback_warns_but_starts(caplog):
    """prod 无降级链 → 告警而非 fail-fast（降级链也可能由代码内置）。"""
    with caplog.at_level("WARNING", logger="app.config.settings"):
        s = Settings(
            env="prod",
            database_url="postgresql://u:p@localhost:5432/db",
            llm_api_key=SecretStr("sk-LEAK-CANARY-999"),
            # 显式清空：仓库 .env 里若配了 LLM_FALLBACK，这条用例测的就不是「无降级链」了
            llm_fallback="",
        )
    assert s.env == "prod"
    joined = " | ".join(r.getMessage() for r in caplog.records)
    assert "llm_fallback" in joined
    assert "sk-LEAK-CANARY-999" not in joined, "告警泄露了密钥"
    # 回归：这条日志没有 %-插值参数，百分号必须按字面印出（曾误写 %%，输出成 99.9%%）
    assert "99.9%" in joined and "99.9%%" not in joined


def test_prod_with_fallback_does_not_warn(caplog):
    """配齐降级链（模型 + **独立密钥**）→ 不该有 fallback 相关告警。"""
    with caplog.at_level("WARNING", logger="app.config.settings"):
        Settings(
            env="prod",
            database_url="postgresql://u:p@localhost:5432/db",
            llm_api_key=SecretStr("sk-x"),
            llm_fallback="gpt-4o",
            llm_fallback_api_key=SecretStr("sk-fallback-y"),
        )
    assert not [r for r in caplog.records if "llm_fallback" in r.getMessage()]


# --- 降级模型的独立密钥（docs/runbook/5.llm-runbook.md §1.2.1） ---


def test_prod_rejects_fallback_without_its_own_key():
    """prod：配了 fallback 却没有 fallback 的独立密钥 → **拒绝启动**。

    理由：降级链跨厂商时复用主密钥必然 401，而 401 属「不可恢复」→ 不重试也不再降级，
    可用性反而比不配降级更差。prod 的告警没人看，所以必须 fail-fast。
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            env="prod",
            database_url="postgresql://u:p@localhost:5432/db",
            llm_api_key=SecretStr("sk-primary"),
            llm_fallback="gpt-4o",
            llm_fallback_api_key=SecretStr(""),
        )
    message = str(excinfo.value)
    assert "llm_fallback_api_key" in message
    assert "sk-primary" not in message, "报错里不得出现密钥明文"


def test_dev_warns_when_fallback_has_no_own_key(caplog):
    """dev：同样情形只**告警**并正常构造（开发时常先配模型名、后配密钥）。"""
    with caplog.at_level("WARNING", logger="app.config.settings"):
        s = Settings(
            llm_primary="deepseek/deepseek-flash",
            llm_fallback="gpt-4o",
            llm_api_key=SecretStr("sk-primary"),
            llm_fallback_api_key=SecretStr(""),
            _env_file=None,  # 不读仓库 .env：这条用例只测关系，不测本机配置
        )
    assert s.llm_fallback == "gpt-4o"
    joined = " | ".join(r.getMessage() for r in caplog.records)
    assert "LLM_FALLBACK_API_KEY" in joined
    assert "401" in joined, "告警要说清后果（跨厂商 401），否则没人会去补这把钥匙"


def test_same_key_for_primary_and_fallback_warns_but_works(caplog):
    """两把 key 相同 → 可以工作，但**没有故障隔离**，应当告警（不是错误）。"""
    with caplog.at_level("WARNING", logger="app.config.settings"):
        s = Settings(
            llm_primary="deepseek/deepseek-flash",
            llm_fallback="deepseek/deepseek-chat",
            llm_api_key=SecretStr("sk-same"),
            llm_fallback_api_key=SecretStr("sk-same"),
            _env_file=None,
        )
    assert s.llm_fallback_api_key.get_secret_value() == "sk-same"
    joined = " | ".join(r.getMessage() for r in caplog.records)
    assert "故障隔离" in joined
    assert "sk-same" not in joined, "告警泄露了密钥"


def test_fallback_key_is_masked_everywhere():
    """新密钥字段必须走既有的掩码出口（repr/str/json/dump_for_logging）。"""
    s = Settings(
        llm_primary="a",
        llm_fallback="b",
        llm_api_key=SecretStr("sk-PRIMARY-CANARY"),
        llm_fallback_api_key=SecretStr("sk-FALLBACK-CANARY"),
        embedding_api_key=SecretStr("sk-EMBED-CANARY"),
        _env_file=None,
    )
    for rendered in (repr(s), str(s), s.model_dump_json(), json.dumps(s.dump_for_logging())):
        for canary in ("sk-PRIMARY-CANARY", "sk-FALLBACK-CANARY", "sk-EMBED-CANARY"):
            assert canary not in rendered, f"{canary} 泄漏到了 {rendered[:80]}"
    # 功能出口仍必须是真值（否则连不上/调不通）
    assert s.llm_fallback_api_key.get_secret_value() == "sk-FALLBACK-CANARY"


def test_embedding_backend_only_accepts_two_values():
    """embedding_backend 是 Literal：写错值启动即失败（而不是等到第一次检索）。

    ⚠️ 每条都要显式给 `llm_primary` + `_env_file=None`：`Settings()` 不读 .env 时
    `llm_primary` 是空串，会被「主模型不许为空」先拦下 —— 那时这条用例测的就不是 backend 了。
    """
    base = {"llm_primary": "deepseek/deepseek-flash", "_env_file": None}
    assert Settings(embedding_backend="litellm", **base).embedding_backend == "litellm"
    assert Settings(embedding_backend="local", **base).embedding_backend == "local"
    with pytest.raises(ValidationError):
        Settings(embedding_backend="sentence-transformers", **base)
    with pytest.raises(ValidationError):
        Settings(embedding_backend="", **base)


def test_embedding_dim_defaults_to_zero_meaning_no_check(monkeypatch):
    """embedding_dim=0 表示「不校验维度」；正数表示强校验（守卫在 client 侧）；负数直接拒绝。

    ⚠️ 断言「默认值」的用例**必须同时挡住 .env 与环境变量**（实测踩到两次）：
      · 不挡 `.env`：有人按文档把 `EMBEDDING_DIM` 设成 1024 后，用例报 `assert 1024 == 0`；
      · 只挡 `.env`（`_env_file=None`）**还不够**：环境变量优先级本来就高于 `.env` 与默认值，
        本机若设了 `$env:EMBEDDING_DIM`，`Settings(_env_file=None)` 依然会拿到 1024 ——
        实测就是这么红的。所以还要 `delenv`。
    这条也顺带说明：`_env_file=None` 只关掉「文件来源」，不等于「干净环境」。
    """
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)

    # 默认值本身也应该被钉住（读的是字段定义，不依赖任何 .env / 环境变量）
    assert Settings.model_fields["embedding_dim"].default == 0

    base = {"llm_primary": "deepseek/deepseek-flash", "_env_file": None}
    assert Settings(**base).embedding_dim == 0, "既无环境变量也无 .env 时必须是默认值 0（= 不校验）"
    assert Settings(embedding_dim=1024, **base).embedding_dim == 1024  # 显式给值就走显式值

    with pytest.raises(ValidationError):
        Settings(embedding_dim=-1, **base)





