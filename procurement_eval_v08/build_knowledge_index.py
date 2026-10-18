"""构建或更新审校通过的采购知识索引。"""
from __future__ import annotations

import argparse
import json

from knowledge_rag import build_default_index


def main() -> None:
    parser = argparse.ArgumentParser(description="构建采购 Agent 中文知识索引")
    parser.add_argument("--force", action="store_true", help="忽略指纹并强制重建")
    args = parser.parse_args()
    _, result = build_default_index(force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
