#!/usr/bin/env python3
"""Check/seal an independent adapter before building its wheel."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from jsonschema import Draft202012Validator
from app.execution.adapter_packages import seal_manifest
from app.execution.v2.registry import load_schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    document = json.loads(args.manifest.read_text())
    Draft202012Validator(load_schema("pack-manifest-v2.schema.json")).validate(document)
    expected = seal_manifest(document, args.manifest.parent)
    if args.update:
        args.manifest.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
    elif expected != document:
        parser.error("adapter digest changed; review source and run --update")
    print(f"{document['pack_id']}@{document['pack_version']}: checked")


if __name__ == "__main__":
    main()
