"""`eval/` —— **`14.eval.md` §2 / §3 里那个路径的薄壳**。

```bash
uv run python -m eval.run --suite rag          # 与文档逐字一致
uv run python -m eval.run --suite agent
uv run python -m eval.run --suite compare      # 单 Agent vs 多 Agent
```

## 这个包为什么只有一行转发

`14.eval.md` §2 的接口写的是 `python -m eval.run`，§3 写的数据在 `eval/datasets/*.json`。
本仓把**实现**放在 `app/eval/`（理由见 `app/eval/__init__.py` 的 docstring），
把**数据**留在这里的 `datasets/` 下 —— 于是需要一层薄壳，让文档里那条命令逐字可跑。

⚠️ 薄壳里**不允许**出现任何逻辑：一旦有人在这里加一句「顺手处理一下」，
就会有两份入口行为不同（而 `-m eval.run` 与 `-m app.eval.run` 是同一件事，必须行为一致）。
判断标准很简单：这个文件应该永远只有 import 与 `sys.exit(main())` 两行代码。
"""
