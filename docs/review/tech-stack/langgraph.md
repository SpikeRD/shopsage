# LangGraph（Agent 编排）

## 本项目用它解决什么

把"路由 → 取数 → 合成 → 护栏"这几个步骤装配成一张**有状态、可持久化、能从断点重放**的图，并且每个节点都能单独测试。

## 实际用法（代码片段 + 文件:行号）

### 1）装配：节点 + 边 + 条件边

```python
# app/graph/builder.py:104-144
builder = StateGraph(GraphStateDoc)
for name, factory in specs.items():
    if name == NODE_GATE and not runtime.interrupts.enabled:
        continue                      # 闸门只在开了策略的图上出现
    builder.add_node(name, factory(runtime))

builder.add_edge(START, NODE_GUARDRAIL_INPUT)
builder.add_conditional_edges(
    NODE_GUARDRAIL_INPUT, after_guardrail_input, {NODE_ROUTER: NODE_ROUTER, END: END}
)
builder.add_conditional_edges(NODE_ROUTER, route_by_intent, _route_map())
for producer in (NODE_RAG, NODE_ANALYSIS, NODE_REFUND_REQUEST):
    builder.add_edge(producer, NODE_SYNTHESIS)
builder.add_conditional_edges(
    NODE_DATA, after_data,
    {NODE_ANALYSIS: NODE_ANALYSIS, NODE_SYNTHESIS: NODE_SYNTHESIS, END: END},
)
builder.add_edge(NODE_SYNTHESIS, NODE_GUARDRAIL_OUTPUT)
builder.add_conditional_edges(NODE_GUARDRAIL_OUTPUT, after_guardrail_output, {END: END})
```

### 2）条件边目标映射：键必须是**节点名**

```python
# app/graph/builder.py:213-214
targets = set(ROUTE_NODES.values()) | {NODE_SYNTHESIS}
return {node: node for node in targets}
```

`route_by_intent` 返回的是**节点名**（`app/graph/routing.py:58` 的 `ROUTE_NODES.get(route)`），所以映射表的键也必须是节点名。
写成 `{**ROUTE_NODES, NODE_SYNTHESIS: NODE_SYNTHESIS}` 时，`data`/`rag`/`synthesis` 恰好键值相同所以不暴露，但 `route=refund` 返回 `"refund_request"` 不在键里 → 运行期 `KeyError`（`app/graph/builder.py:181-212` 有完整复盘）。

### 3）`durability="sync"`：钉进每一次调用

```python
# app/graph/builder.py:89
TURN_DURABILITY: str = "sync"

# app/graph/builder.py:166-175
def graph_config(thread_id: str, **extra: Any) -> dict[str, Any]:
    config = thread_config(thread_id, **extra)
    config["durability"] = TURN_DURABILITY
    return config
```

### 4）流式驱动 + 断点重放

```python
# app/graph/run.py:154-163
async for chunk in graph.astream(payload, config, stream_mode="updates"):
    for node, update in (chunk or {}).items():
        if node == INTERRUPT_KEY:       # 闸门挂起的载荷不在任何节点增量里
            turn.interrupt = _interrupt_payload(update)
            break
        turn.visited.append(str(node))
        if on_event is not None:
            on_event(str(node), update)
```

```python
# app/graph/run.py:175-192（失败 → 读 next → 重放）
snapshot = await graph.aget_state(config)
pending = tuple(snapshot.next or ())
if not pending:
    break
target = _failed_node(exc, pending, turn.visited)
if replays_left > 0:
    replays_left -= 1
    payload = None                       # None = 从 checkpoint 继续
    continue
```

```python
# app/graph/run.py:206（兜底时必须 as_node，否则无限重放）
await graph.aupdate_state(config, update, as_node=target)
```

### 5）checkpointer 三后端

```python
# app/graph/checkpointer.py:123-142
if backend == "memory":
    yield InMemorySaver()
if backend == "postgres":
    async with _open_postgres(url, setup=setup) as saver:   # AsyncPostgresSaver
        yield saver
if backend == "redis":
    async with _open_redis(url, setup=setup) as saver:
        yield saver
```

## 最小原理

1. **`StateGraph(TypedDict)`**：图的"状态"是一个带 reducer 的 TypedDict，每个节点返回**增量 dict**，框架负责合并（`messages` 用 `add_messages`）。
2. **节点 = 可 await 的函数 `(state) -> dict`**；**边 = 固定转移**；**条件边 = 一个返回"目的地名"的函数**，返回值必须出现在 `add_conditional_edges` 的映射表键里。
3. **checkpointer 在每个 super-step 结束时写一份快照**，键是 `thread_id`。`durability` 决定"先返回还是先落盘"：`async`（默认，先返回）与 `sync`（先落盘）。
4. **重放**：节点抛异常时框架不推进 `next`，所以你拿到的是"卡在哪个节点"的快照；用 `ainvoke(None, config)` 或 `astream(None, config)` 从那里继续，已完成的节点不会重跑。
5. **`interrupt()`**：抛一个控制流异常挂起图，恢复时**整个函数从头重跑**，所以状态只能写在"有结论"的分支里（`app/graph/nodes.py:385-393`）。

## 常见坑

| 坑 | 症状 | 本项目怎么绕开 |
|---|---|---|
| 条件边映射漏 `END` | `get_graph()` 排序时 `TypeError: '<' not supported between NoneType and str` | 映射里**显式**写 `{END: END}`（`app/graph/builder.py:140-144`） |
| 用 `add_node(error_handler=...)` | 同上（框架生成的内部边标签是 `None`）→ 图结构不可断言 | 兜底改成节点内 try/except（`app/graph/nodes.py:21-31`） |
| 节点内 `except` 后返回兜底 dict | 框架认为这一步成功 → `next` 变空 → **断点丢了** | `safe_node` 刻意不 catch（`app/graph/nodes.py:180-185`） |
| `durability` 用默认异步 | 进程崩溃时"节点跑了、checkpoint 没写" | 钉 `sync` |
| `aupdate_state` 不传 `as_node` | 失败节点又被排进 `next` → 无限重放 | 传 `as_node=target` |
| Windows 上 psycopg 异步 | `ProactorEventLoop` 不支持 → 报错 | `ensure_compatible_event_loop()`（`app/graph/checkpointer.py:91`） |
| 在 `interrupt()` 之前写状态 | 恢复时函数重跑 → 写两遍或丢失 | 只在有结论的分支写（`app/graph/nodes.py:385-393`） |

## 深入关键词

`StateGraph` · `add_conditional_edges` · `astream(stream_mode="updates")` · `durability` · `BaseCheckpointSaver` · `AsyncPostgresSaver` · `InMemorySaver` · `aget_state` / `aupdate_state` · `Interrupt` / `Command(resume=...)` · `add_messages` reducer · super-step · `GraphRecursionError`
