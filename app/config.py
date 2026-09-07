"""运行配置：全部惰性读取环境变量，便于测试 monkeypatch。

默认绑定回环地址（个人单用户场景的安全默认），DATA_DIR/HOST/PORT 可覆盖。
"""

import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_DATA_DIR = "./data"
DB_FILENAME = "tiku.db"


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def host() -> str:
    return os.environ.get("HOST", DEFAULT_HOST)


def port() -> int:
    return int(os.environ.get("PORT", DEFAULT_PORT))


def fake_llm_enabled() -> bool:
    """TIKU_FAKE_LLM=1 时用 EchoLLM 冒烟（M1 协议联通验证用）。"""
    return os.environ.get("TIKU_FAKE_LLM", "") == "1"
