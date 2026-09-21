"""分块器:把日志/工具输出切成 ~N 行的块,并提供**可逆 stub 存储**。

设计(winnow 式):

- 大块文本按行切成固定行数(默认 25)的块,保留原始行号区间,**行数守恒**(不丢行);
- 每个块存入 :class:`StubStore`,上下文里只放 stub(restore key + 首行摘要),
  需要时用 restore key 完整还原原文 —— 裁判「丢掉」的块永远可以找回,绝不丢数据;
- stub 文本刻意极短,让 token 节省可测量(JEV2 的 replay.py 对照原始 vs 筛后)。

只依赖标准库。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

DEFAULT_CHUNK_LINES = 25


@dataclass(frozen=True)
class Chunk:
    """一个日志块。``lines`` 为原文行(不含换行符),``start_line``/``end_line`` 为
    1-indexed 闭区间。"""

    key: str
    index: int
    start_line: int
    end_line: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """还原为带换行的原文片段(与 split 前的对应段逐字符一致,含末尾换行)。"""
        return "\n".join(self.lines) + "\n" if self.lines else ""

    @property
    def n_lines(self) -> int:
        return len(self.lines)

    @property
    def n_chars(self) -> int:
        return len(self.text)

    @property
    def approx_tokens(self) -> int:
        """粗略 token 估算:英文 ~4 字符/token,中文按 1 字符 ≈ 1 token 的上界口径,
        这里统一用 chars/3 向上取整(PoC 对照测量两侧同口径即可)。"""
        return max(1, -(-self.n_chars // 3))


@dataclass
class StubStore:
    """块存储:stub ↔ 原文可逆。

    - :meth:`put` 存块,返回 restore key(内容哈希,幂等);
    - :meth:`restore` 按 key 还原全文;
    - :meth:`stub_text` 生成放进上下文的极短 stub。
    """

    _chunks: dict = field(default_factory=dict)

    def put(self, chunk: Chunk) -> str:
        self._chunks[chunk.key] = chunk
        return chunk.key

    def restore(self, key: str) -> Optional[str]:
        chunk = self._chunks.get(key)
        return None if chunk is None else chunk.text

    def has(self, key: str) -> bool:
        return key in self._chunks

    def __len__(self) -> int:
        return len(self._chunks)

    @staticmethod
    def stub_text(chunk: Chunk) -> str:
        """上下文里的替身:一行摘要 + restore key。首行截断防超长行反噬。"""
        first = chunk.lines[0][:80] if chunk.lines else "(empty)"
        return f"[jev-stub {chunk.key} lines={chunk.start_line}-{chunk.end_line}] {first}…"


def chunk_key(index: int, text_hash: str) -> str:
    """restore key:短、稳定、可打印。index 前缀防同文异块碰撞歧义。"""
    return f"c{index:04d}-{text_hash[:10]}"


def split_text(
    text: str,
    *,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    store: Optional[StubStore] = None,
) -> List[Chunk]:
    """把 ``text`` 按行切成 ``chunk_lines`` 行的块。

    - 行数守恒:所有块行数之和 == 原文行数(空文本 -> 0 块;结尾无换行不多算空行);
    - 每个块自动 :meth:`StubStore.put`(给了 store 时),保证 stub 可逆;
    - 超长单行不折行(日志行本来就长,折行破坏逐字符还原)。
    """
    if chunk_lines < 1:
        raise ValueError("chunk_lines 必须 >= 1")
    if not text:
        return []
    # keepends 切分保证行数守恒且拼接可逐字符还原
    raw_lines = text.splitlines()
    chunks: List[Chunk] = []
    for index, start in enumerate(range(0, len(raw_lines), chunk_lines)):
        lines = tuple(raw_lines[start : start + chunk_lines])
        body = "\n".join(lines)
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
        chunk = Chunk(
            key=chunk_key(index, digest),
            index=index,
            start_line=start + 1,
            end_line=start + len(lines),
            lines=lines,
        )
        if store is not None:
            store.put(chunk)
        chunks.append(chunk)
    return chunks


def iter_stubbed(
    text: str, *, chunk_lines: int = DEFAULT_CHUNK_LINES, store: Optional[StubStore] = None
) -> Iterator[str]:
    """便捷生成器:切块并逐块 yield stub 文本(裁判场景直接喂 stub)。"""
    if store is None:
        store = StubStore()
    for chunk in split_text(text, chunk_lines=chunk_lines, store=store):
        yield StubStore.stub_text(chunk)


def total_lines(chunks: Sequence[Chunk]) -> int:
    """行数守恒校验助手:所有块行数之和。"""
    return sum(c.n_lines for c in chunks)
