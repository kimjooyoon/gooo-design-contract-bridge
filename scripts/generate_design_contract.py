#!/usr/bin/env python3
"""Generate design tokens and CSS from the product's Gooo semantic source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


BUNDLE_FILES = [
    "design-tokens.json",
    "component.css",
    "generation-receipt.json",
    "claim-graph.json",
    "manifest.json",
    "provenance.json",
    "design-tokens.sha256",
    "component.css.sha256",
]
ACTIVITY_RE = re.compile(
    r'^activity\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s+->\s+'
    r'(?P<result>[A-Za-z_][A-Za-z0-9_]*)\s+computes\s+"(?P<program>[^"]+)"$'
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical(value).encode("utf-8"))


def hex_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> bytes:
    data = (canonical(value) + "\n").encode("utf-8")
    path.write_bytes(data)
    return data


def parse_program(program: str) -> dict[str, str]:
    parts = program.split(";")
    if not parts or parts[0] not in {"design.token:v2", "design.projection:v2", "design.reader:v2", "design.guardrail:v2"}:
        raise ValueError(f"unsupported Gooo design program: {program}")
    result = {"kind": parts[0]}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator or not key or not value:
            raise ValueError(f"malformed Gooo design program field: {part}")
        if key in result:
            raise ValueError(f"duplicate Gooo design program field: {key}")
        result[key] = value
    return result


def parse_source(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = ACTIVITY_RE.match(line.strip())
        if match:
            records.append({
                "activity": match.group("name"),
                "result": match.group("result"),
                "program": match.group("program"),
                "program_digest": digest_bytes(match.group("program").encode("utf-8")),
                "line": line_number,
            })
    if len(records) != 12 or len({item["activity"] for item in records}) != 12:
        raise ValueError("the product Gooo source must contain twelve unique computed activities")
    for item in records:
        item["meaning"] = parse_program(item["program"])
    return records


def ensure_outside(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    raise ValueError("product generation output must be caller-owned and outside the repository")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--denominator", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.repo_root.resolve()
    source_path = args.source.resolve()
    ir_path = args.ir.resolve()
    denominator_path = args.denominator.resolve()
    output = args.output.resolve()
    for path in (source_path, ir_path, denominator_path):
        path.relative_to(root)
    ensure_outside(output, root)

    source_bytes = source_path.read_bytes()
    source_digest = digest_bytes(source_bytes)
    ir = load_json(ir_path)
    denominator = load_json(denominator_path)
    records = parse_source(source_path)
    activities = {item["activity"]: item for item in records}
    graph_activity_names = {
        node["name"]
        for node in ir.get("nodes", [])
        if str(node.get("kind", "")).lower() == "activity"
    }
    expected_activity_names = {cell["activity"] for cell in denominator["cells"]}
    if graph_activity_names != expected_activity_names:
        raise ValueError("released Gooo IR activities do not equal the product denominator")
    if ir.get("schema_version") != "gooo-graph/v1" or ir.get("ir", {}).get("status") != "available":
        raise ValueError("released Gooo IR is not an available graph dump")
    ir_digest = "sha256:" + str(ir["ir"]["semantic_digest"])
    graph_digest = "sha256:" + hashlib.sha256(ir_path.read_bytes()).hexdigest()

    token_records = []
    for record in records:
        meaning = record["meaning"]
        if meaning["kind"] != "design.token:v2":
            continue
        required = {"cell", "proof", "indicator", "axis", "name", "value", "property", "role", "claim"}
        if not required.issubset(meaning):
            raise ValueError(f"token declaration is incomplete: {record['activity']}")
        cell = next((item for item in denominator["cells"] if item["id"] == meaning["cell"]), None)
        if cell is None or cell["activity"] != record["activity"]:
            raise ValueError(f"token declaration is not bound to its denominator cell: {record['activity']}")
        if cell["proof"] != meaning["proof"] or cell["indicator"] != meaning["indicator"] or cell["axis"] != meaning["axis"]:
            raise ValueError(f"token declaration metadata disagrees with denominator: {record['activity']}")
        token_records.append({
            "name": meaning["name"],
            "value": meaning["value"],
            "property": meaning["property"],
            "role": meaning["role"],
            "claim_id": meaning["claim"],
            "source_activity": record["activity"],
            "source_line": record["line"],
            "source_program_digest": record["program_digest"],
        })
    if len(token_records) != 4 or len({item["name"] for item in token_records}) != 4:
        raise ValueError("the Gooo design source must declare four unique token intents")
    token_records.sort(key=lambda item: item["name"])

    output.mkdir(parents=True, exist_ok=True)
    token_document = {
        "schema": "gooo/design-tokens/v2",
        "authority": "gooo-source:designcontractbridge",
        "source": {"path": str(source_path.relative_to(root)), "digest": source_digest},
        "semantic_ir": {"path": str(ir_path), "digest": ir_digest},
        "tokens": token_records,
    }
    token_bytes = write_json(output / "design-tokens.json", token_document)

    css_lines = [
        "/* generated by the Gooo design contract; semantic source is authoritative */",
        ":root {",
    ]
    for token in token_records:
        css_lines.append(f"  {token['property']}: {token['value']};")
    css_lines.append("}")
    css_bytes = ("\n".join(css_lines) + "\n").encode("utf-8")
    (output / "component.css").write_bytes(css_bytes)

    generated_refs = ["design-tokens.json", "component.css", "generation-receipt.json", "claim-graph.json"]
    claims = []
    for token in token_records:
        claim_body = {
            "claim_id": token["claim_id"],
            "activity": token["source_activity"],
            "state": "CLOSED",
            "statement": f"Gooo intent {token['name']} projects to {token['property']} with declared role {token['role']}",
            "source_intent": {"name": token["name"], "value": token["value"], "role": token["role"]},
            "generated_artifacts": generated_refs,
            "source_digest": source_digest,
            "ir_digest": ir_digest,
        }
        claims.append({**claim_body, "claim_digest": digest_value(claim_body)})
    claim_graph = {
        "schema": "gooo/design-contract-bridge/claim-graph/v2",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "claims": claims,
        "generated_artifacts": generated_refs,
    }
    claim_graph_bytes = write_json(output / "claim-graph.json", claim_graph)

    receipt = {
        "schema": "gooo/design-contract-bridge/generation-receipt/v2",
        "authority": "gooo-source:designcontractbridge",
        "generator": "product-owned-design-contract-generator",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "activity_count": len(records),
        "token_count": len(token_records),
        "bundle_files": BUNDLE_FILES,
        "generated_artifact_digests": {
            "design-tokens.json": digest_bytes(token_bytes),
            "component.css": digest_bytes(css_bytes),
            "claim-graph.json": digest_bytes(claim_graph_bytes),
        },
        "permission": {"repository_writes": 0, "output_scope": "caller-owned"},
    }
    write_json(output / "generation-receipt.json", receipt)
    manifest = {
        "schema": "gooo/design-contract-bridge/generated-envelope/v2",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "files": BUNDLE_FILES,
        "activities": [item["activity"] for item in records],
    }
    write_json(output / "manifest.json", manifest)
    provenance = {
        "schema": "gooo/design-contract-bridge/provenance/v2",
        "authority": "contracts/design-token-bridge.gooo",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "generated_by": "product-owned-design-contract-generator",
        "permission": {"repository_writes": 0, "output_scope": "caller-owned"},
    }
    write_json(output / "provenance.json", provenance)
    (output / "design-tokens.sha256").write_text(f"{hex_digest(token_bytes)}  design-tokens.json\n", encoding="utf-8")
    (output / "component.css.sha256").write_text(f"{hex_digest(css_bytes)}  component.css\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"design generator: {exc}")
        raise SystemExit(1)
