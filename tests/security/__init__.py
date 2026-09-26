"""安全模块的测试包（第 4 步）。

有 `__init__.py` 是有意的（而不是靠 pytest 的 rootdir 推断）：
`app/security/__init__.py` 与它同名，显式成包可以避免「同名模块到底是 app 的还是 tests 的」这类
导入歧义 —— 本仓在 Windows 上跑，路径大小写/`sys.path` 的差异值得提前消除。
"""
