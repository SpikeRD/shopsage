"""一次性探针：看一眼 rag_chunks 的来源文档与片段 id 形状（给第 16 步的溯源展示用）。

为什么单独一个文件而不是 `python -c`：Windows 控制台 + PowerShell 的多层引号
会把 SQL 里的单引号吃掉，实测输出为空（看起来像「表是空的」而其实语句没跑对）。
写成文件是**排除引号问题**的标准做法。
"""

from __future__ import annotations

from sqlalchemy import text

from app.db.session import engine


def main() -> None:
    with engine.connect() as conn:
        total = conn.execute(text("select count(*) from rag_chunks")).scalar_one()
        active = conn.execute(text("select count(*) from rag_chunks where is_active")).scalar_one()
        print(f"rag_chunks 总数={total} 活跃={active}")
        rows = conn.execute(
            text(
                "select source, count(*) as n, min(chunk_id) as first_chunk "
                "from rag_chunks group by source order by source limit 12"
            )
        ).all()
        for source, count, first_chunk in rows:
            print(f"  {source} | {count} 片段 | 首个 chunk_id={first_chunk}")


if __name__ == "__main__":
    main()
