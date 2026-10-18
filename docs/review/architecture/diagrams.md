# 架构图集（Mermaid）

> **约定**：实线 = 代码里能找到对应模块/类；虚线 + `?` = 存在但本次无法在代码里确证的结构。
> 每个节点括号里是**代码位置**，均可回查。四个图分别回答：谁在用（上下文）、怎么分层（依赖）、一次请求怎么走（时序）、图内部拓扑。

---

## 1. 系统上下文图

```mermaid
flowchart LR
    C["客户 customer"]
    E["员工 internal_employee / manager<br/>finance_auditor / customer_service"]
    A["管理员 admin"]

    subgraph FE["前端 web/ Next 15.5.27"]
        WEB["App Router 页面<br/>app/(app)/page.tsx<br/>internal/ internal/refunds/<br/>orders/ admin/"]
        LOGIN["登录页<br/>app/(auth)/login/page.tsx"]
        KBVIEW["出处查看<br/>app/api/kb/route.ts"]
    end

    subgraph BE["后端 app/ FastAPI + LangGraph"]
        API["API 层<br/>app/api/main.py create_app"]
        GRAPH["图编排<br/>app/graph/builder.py build_graph"]
        AG["7 个节点<br/>app/agents/*.py"]
        TOOLS["工具层<br/>app/tools/__init__.py"]
        LLM["LLM 门面<br/>app/llm/client.py"]
    end

    subgraph EXT["外部与基础设施"]
        DB[("PostgreSQL + pgvector<br/>10 张表")]
        RD[("Redis<br/>限流 / 队列 / 缓存快路径")]
        VDB[("rag_chunks.vector(1024)<br/>HNSW 索引")]
        PROV["LLM / Embedding 厂商<br/>deepseek · dashscope"]
        OBS["Prometheus · Grafana<br/>OTel Collector · Langfuse"]
        MCPC["MCP 客户端<br/>Cursor · Claude Desktop"]
    end

    C --> WEB
    E --> WEB
    A --> WEB
    C --> LOGIN
    E --> LOGIN
    A --> LOGIN
    WEB -->|"HTTP + WebSocket<br/>shopsage.v1 + bearer 令牌"| API
    LOGIN -->|"POST /api/auth/login"| API
    KBVIEW -->|"读 docs/kb"| DB
    API --> GRAPH
    GRAPH --> AG
    AG --> TOOLS
    AG --> LLM
    TOOLS --> DB
    TOOLS --> VDB
    AG --> VDB
    API --> RD
    LLM --> PROV
    API --> OBS
    MCPC -->|"stdio / streamable-http<br/>app/mcp/server.py"| TOOLS
```

**读图要点**

- MCP 客户端**不经 API 层**，直接打工具层：`app/mcp/server.py:95` 的 `build_server` → `app/mcp/sync_calls.py` → `app/tools/`。
- 前端**不经过 Next 的服务端路由**转发 WS（`web/next.config.ts` 注释第 2 条：`rewrites` 不代理 WS 升级），所以后端必须 `--host 0.0.0.0`。

---

## 2. 分层模块依赖图

```mermaid
flowchart TD
    subgraph L1["层1 表现层 web/src"]
        w1["app 下 7 个 page.tsx"]
        w2["lib/ auth · session · stream<br/>contracts · chart"]
        w3["components/ SideNav · EChart<br/>MessageList · useChatStream"]
        w1 --> w2
        w1 --> w3
    end

    subgraph L2["层2 接口层 app/api"]
        a1["main.py create_app"]
        a2["middleware.py 限流<br/>scope.py · ratelimit.py"]
        a3["deps.py Actor 与 thread_id"]
        a4["auth · admin · chat · orders<br/>refunds · tasks · health"]
        a1 --> a2
        a1 --> a4
        a4 --> a3
    end

    subgraph L3["层3 编排层 app/graph"]
        g1["builder.py 拓扑 + durability"]
        g2["nodes.py 7 个节点工厂"]
        g3["routing.py 条件边"]
        g4["run.py run_turn 重放与兜底"]
        g5["checkpointer.py 三后端"]
        g1 --> g2
        g1 --> g3
        g4 --> g1
        g5 --> g1
    end

    subgraph L4["层4 节点层 app/agents"]
        n0["state.py inject_identity<br/>agent_llm.py generate_json"]
        n1["router.py"]
        n2["data.py"]
        n3["rag.py"]
        n4["analysis.py"]
        n5["refund.py"]
        n6["synthesis.py"]
        n7["guardrail.py"]
        n1 --> n0
        n2 --> n0
        n3 --> n0
        n6 --> n0
        n7 --> n0
    end

    subgraph L5["层5 能力层"]
        t1["app/tools 27 个出口"]
        l1["app/llm/client.py<br/>llm_call / embed"]
        r1["app/rag/retriever.py<br/>hybrid_search"]
        c1["app/cache/api.py<br/>lookup / remember"]
        q1["app/queue arq 队列"]
        ev1["app/eval 评测"]
    end

    subgraph L6["层6 基础层"]
        d1["app/db session · models · queries"]
        s1["app/security rbac · policy · masking<br/>auth · accounts · sql_guard · guardrail"]
        cfg["app/config/settings.py<br/>+ app/api/config.py 的 API_*"]
        o1["app/observability"]
        mc["app/mcp"]
    end

    L2 --> L3
    L3 --> L4
    L4 --> L5
    L5 --> L6
    L2 --> L6
    L4 --> L6
    mc --> t1
    mc --> s1
    ev1 --> L3
    ev1 --> r1
    o1 -.->|"由 nodes.safe_node 统一埋点<br/>业务节点不反向依赖"| L4
```

**三条写在代码里的依赖铁律**

1. `app/eval` **不 import FastAPI**（`docs/runbook/14.eval-runbook.md` §2.4）。
2. `app/mcp` **不 import `app.api` / fastapi / uvicorn / starlette**（`docs/runbook/15.mcp-runbook.md` §2.4）。
3. 六个 Agent 模块**不知道可观测性存在**：span 只在 `app/graph/nodes.py:215` 的 `safe_node` 里统一开（同文件 :191-204 有理由）。

---

## 3. 一次请求时序图（对客问答，WS 流式）

```mermaid
sequenceDiagram
    autonumber
    participant Br as 浏览器 stream.ts
    participant WS as chat.py chat_stream
    participant Dep as deps.py ws_actor
    participant RT as run.py run_turn
    participant GI as guardrail 输入侧
    participant RS as router
    participant DN as data
    participant TN as synthesis
    participant CA as cache/api
    participant GO as guardrail 输出侧
    participant CK as checkpointer

    Br->>WS: 握手 子协议 shopsage.v1 与 bearer 令牌
    WS->>Dep: ws_actor(websocket)
    Dep->>Dep: principal_with_account 查 accounts.token_version
    Dep-->>WS: Actor user_id 与 role 与 thread_id
    WS->>WS: accept(subprotocol=shopsage.v1)
    WS->>WS: 限流判定 WS_SCOPE_CHAT_STREAM
    Note over WS,Br: 未通过则发 error 帧 并关闭 4429
    WS-->>Br: skeleton 帧 phase=accepted 首字节
    WS->>RT: run_turn(graph, entry_state, thread_id)
    RT->>CK: checkpoint durability=sync
    RT->>GI: guardrail_input(state)
    GI-->>RT: blocked=false
    RT->>RS: router(state, llm)
    RS-->>RT: route=data 且 permissions 已注入
    RT->>DN: data(state, llm, row_limit, max_attempts)
    DN->>DN: plan_sql 然后 validate_sql 然后 execute_readonly_query
    DN-->>RT: rows 与 sql 与 row_meta
    RT->>TN: synthesis(state, llm, cache)
    TN->>CA: lookup(messages, user_id, role, params)
    CA-->>TN: hit=false 或命中则直接用缓存答案
    TN->>CA: remember 空结果不写
    TN-->>RT: answer 与 citations
    RT->>GO: guardrail_output(state, stage=output)
    GO-->>RT: blocked=false 且追加 assistant 消息
    RT->>CK: checkpoint 落盘
    RT-->>WS: Turn(visited, blocked, replayed)
    WS-->>Br: node 帧 若干 含 skeleton phase=routing
    WS-->>Br: answer 帧 text 与 citations 与 chart
    WS-->>Br: done 帧 flow_state 与 first_frame_ms
```

**三个"不看代码想不到"的点**

1. **骨架帧在跑图之前发** —— 所以"首帧 <1s"与模型快慢无关（`app/api/chat.py:548-551`）。
2. **node 帧是跑完图之后批量补发的**，不是实时的（`app/api/chat.py:553-590`）：`run_turn` 的 `on_event` 是同步回调，回调里 `await` 不了。
3. **checkpoint 用 `durability="sync"`** —— 每步都落盘，断点重放才成立（`app/graph/builder.py:89`）。

---

## 4. 图内部拓扑（与 `consolidated/modules/08-graph.md` 同源）

```mermaid
flowchart TD
    S([START]) --> GI["guardrail_input<br/>零 token"]
    GI -->|blocked| E1([END])
    GI --> R["router"]
    R --> D["data"]
    R --> G["rag"]
    R --> A["analysis"]
    R --> RQ["refund_request<br/>唯一写节点 无 LLM"]
    R --> SY["synthesis"]
    D -->|route 为 analysis| A
    D -->|其他 route| SY
    D -->|blocked| E2([END])
    G --> SY
    A --> SY
    RQ --> SY
    SY --> GO["guardrail_output<br/>通过时追加 assistant 消息"]
    GO --> E3([END])
    R -.->|"gate 仅在 InterruptPolicy.enabled 时注册"| GT["gate"]
    GT -.-> SY
```

> `gate` 用虚线：**代码里存在**（`app/graph/nodes.py:373` 的 `make_gate`），但默认配置下**不会被注册进图** —— `app/graph/config.py:82` 的 `GATED_ROLES = frozenset()` 为空集，`app/graph/builder.py:108-110` 据此跳过注册。
