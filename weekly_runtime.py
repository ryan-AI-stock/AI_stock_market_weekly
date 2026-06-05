"""週報執行環境與設定檔共用工具。"""

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
TRUE_VALUES = frozenset({"1", "true", "yes", "y"})


def load_config_file(config_path: Path | str | None = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def write_github_output(name: str, value: str, output_path: Path | str | None = None) -> None:
    target = output_path or os.environ.get("GITHUB_OUTPUT", "").strip()
    if target:
        with Path(target).open("a", encoding="utf-8") as output_file:
            output_file.write(f"{name}={value}\n")
    print(f"{name}={value}")
