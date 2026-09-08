"""pytest 路径引导:保证从任意目录运行 ``pytest backend/`` 都能 import ``data_sources``。"""

from __future__ import annotations

import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
