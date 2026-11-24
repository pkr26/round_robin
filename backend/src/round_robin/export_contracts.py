"""Reproducibly export authoritative offline schemas and nonsecret fixtures."""

import argparse
import json
import sys
from pathlib import Path

from .fixture_api import app
from .fixtures import SampleBundle, sample_bundle
from .schemas import DraftBrief, EventEnvelope


def rendered_outputs() -> dict[str, str]:
    from .app import app as application
    outputs = {"openapi.json": app.openapi(), "application.openapi.json": application.openapi(), "events.schema.json": EventEnvelope.model_json_schema(mode="serialization"), "brief.schema.json": DraftBrief.model_json_schema(mode="serialization"), "sample.schema.json": SampleBundle.model_json_schema(mode="serialization"), "sample.json": sample_bundle().model_dump(mode="json")}
    return {filename: json.dumps(content, indent=2, ensure_ascii=False) + "\n" for filename, content in outputs.items()}


def export(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in rendered_outputs().items():
        (directory / filename).write_text(content, encoding="utf-8")


def check(directory: Path) -> bool:
    changed = [filename for filename, content in rendered_outputs().items() if not (directory / filename).is_file() or (directory / filename).read_text(encoding="utf-8") != content]
    if changed:
        print("Contract drift: " + ", ".join(changed), file=sys.stderr)
        return False
    print("Authoritative fixture contracts match committed outputs.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[3] / "contracts")
    parser.add_argument("--check", action="store_true", help="Fail on drift without writing files")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(0 if check(args.output) else 1)
    export(args.output)


if __name__ == "__main__":
    main()
