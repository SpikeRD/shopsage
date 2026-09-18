# api — FastAPI 路由 / WebSocket 流式 / 限流中间件 / JWT 鉴权

## 1. 职责

对外暴露 HTTP / WebSocket 接口：对客（Web/IM）与对内（内部系统）两个入口的请求都从这里进，负责鉴权、限流、参数校验、流式输出，然后转交 `graph.md`。

## 2. 对外接口

```http
POST /api/chat                    # 非流式问答
WS   /api/chat/stream             # WebSocket 流式问答（B2）
GET  /health                      # 健康检查
GET  /metrics                     # Prometheus 指标（observability.md）
```

## 3. 依赖

- `fastapi` + `uvicorn` + `websockets`
- `security/`（JWT 鉴权 `get_current_user`、限流）
- `graph.md`（业务入口）、`config.md`

## 4. 关键设计决策

1. **对客 / 对内走同一 API、靠 JWT 区分角色**：`get_current_user` 解析出 `user_id` / `role`，注入到图 state，权限判断集中在 `security/`（不散落在路由）。
2. **WebSocket 做流式**：先出「结论骨架」再补细节，首 token < 1s（B2）。
3. **限流中间件**：按 `user_id` + 接口限流，防滥用与刷接口（D1 纵深）。
4. **API 层无业务逻辑**：只做「鉴权→校验→转交图→格式化返回」，业务全在 graph/agents，保证可测试、可水平扩容（架构第 8 节无状态化）。

## 5. 核心技术点（要懂为什么）

- **依赖注入（`Depends`）做鉴权**：`get_current_user` 作为依赖，路由函数声明 `user = Depends(get_current_user)` 即可拿到身份，鉴权逻辑一处定义、处处复用。
- **流式输出怎么传**：图内 synthesis 逐 token 产出 → 通过 `astream` 事件推送 WebSocket，前端边收边渲染。
- **限流算法**：滑动窗口/令牌桶，Redis 计数，超过阈值返回 429；与 `llm.md` 的重试区分——这里拦的是「用户请求」，那里重试的是「模型调用」。
- **无状态可扩容**：会话外置（Redis/Postgres checkpoint），API 多副本无需共享内存（架构第 8 节）。

## 6. 开发步骤

1. 写 `app/api/deps.py`：`get_current_user`（JWT 校验）。
2. 写 `app/api/chat.py`：`POST /api/chat`（转交 `graph.ainvoke`）+ `WS /api/chat/stream`（`astream` 推送）。
3. 写 `app/api/middleware.py`：限流中间件。
4. 写 `app/main.py`：装配 FastAPI app + 中间件 + 路由。
5. 写集成测试（TestClient / websocket 客户端）。

## 7. 测试用例

```python
def test_health():                       # GET /health → 200
def test_requires_auth():                # 无 token → 401
def test_chat_returns_answer():          # 带 token 调用 /api/chat 得到回答
def test_streaming_first_token_fast():   # WS 流式，首 token < 1s（B2）
def test_rate_limit():                   # 超过阈值 → 429（D1）
def test_customer_cannot_see_other():    # 越权请求被拒（D1，端到端）
```

## 8. 验收标准

- [ ] `/api/chat` 与 WebSocket 流式均可用，鉴权/限流生效。
- [ ] 首 token 延迟 < 1s（B2）。
- [ ] 越权请求端到端被拦截（D1）。
- [ ] API 无状态，多副本可水平扩容（架构第 8 节）。
- [ ] 证据链接：`PR#__` / `压测报告#__`
