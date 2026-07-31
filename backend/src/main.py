from __future__ import annotations

import argparse
import json

from src.core.pipeline import DailyReportPipeline
from src.db.factory import create_database
from src.llm.factory import create_llm_judge
from src.storage.file_store import FileStorage
from src.utils.config import default_config_path, load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Report V2 CLI")
    parser.add_argument("--config", default=None, help="Path to config.yaml; defaults to project config/config.yaml")
    args = parser.parse_args()
    config = load_config(args.config or str(default_config_path()))
    db = create_database(config)
    storage = FileStorage(config)
    result = DailyReportPipeline(config, db, storage, create_llm_judge(config)).run()
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
