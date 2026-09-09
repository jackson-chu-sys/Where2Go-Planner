"""pytest 引导:``sys.path`` + 全测试集默认关闭种子数据。

两件事:

1. 保证从任意目录运行 ``pytest backend/`` 都能 import ``data_sources`` / ``services``;
2. 用 autouse fixture 把 :data:`services.seed_data.ENV_SEEDS` 设为关闭 —— 种子数据线上默认开,
   但既有单测(TASK-1a/1b 共 87 个)对 ``place_count``/条数有**精确断言**,
   整套默认关掉后这些断言零改动即可继续通过;
   需要种子的用例(:mod:`backend.test_seed_data`)再显式 ``monkeypatch.setenv`` 打开,
   或直接给 :func:`services.place_loader.load_segment` 传自造种子列表。
"""

from __future__ import annotations

import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


@pytest.fixture(autouse=True)
def seeds_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """整套单测默认关闭人工种子数据(线上默认开,见 services.seed_data)。"""
    monkeypatch.setenv("WHERE2GO_SEEDS", "off")
