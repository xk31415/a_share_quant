"""pytest 引导：确保 tests/ 目录在 sys.path 中，``import helpers`` 不依赖
pytest 的 rootdir 插入行为（换 pytest 版本 / IDE 时更稳）。"""
import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
