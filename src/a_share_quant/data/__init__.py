"""数据层：schema 规范、数据加载、质量校验与 point-in-time 查询。"""
from .asof import latest_known
from .loader import DataProvider, LocalFileProvider
from .manifest import describe_dataset, frame_digest, verify_dataset
from .builders import (
    DEFAULT_BOARDS,
    BuildReport,
    assemble,
    drop_nonmonotonic_k,
    classify_board,
    select_boards,
    self_check,
    split_symbol,
)
from .schema import (
    BAR_OPTIONAL_COLUMNS,
    BAR_REQUIRED_COLUMNS,
    INSTRUMENT_REQUIRED_COLUMNS,
    PRICE_USAGE_POLICY,
    STATUS_REQUIRED_COLUMNS,
    coerce_bars,
    coerce_bool,
)
from .validation import DataValidationError, validate_dataset

__all__ = [
    "DataProvider",
    "LocalFileProvider",
    "latest_known",
    "DataValidationError",
    "validate_dataset",
    "BAR_REQUIRED_COLUMNS",
    "BAR_OPTIONAL_COLUMNS",
    "INSTRUMENT_REQUIRED_COLUMNS",
    "STATUS_REQUIRED_COLUMNS",
    "PRICE_USAGE_POLICY",
    "coerce_bars",
    "coerce_bool",
    "BuildReport",
    "assemble",
    "drop_nonmonotonic_k",
    "self_check",
    "split_symbol",
    "classify_board",
    "select_boards",
    "DEFAULT_BOARDS",
    "describe_dataset",
    "verify_dataset",
    "frame_digest",
]
