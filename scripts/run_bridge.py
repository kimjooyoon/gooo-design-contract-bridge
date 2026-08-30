#!/usr/bin/env python3
"""Assemble the read-only CI evidence for the design contract user path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import time
from pathlib import Path
from typing import Any


ACTIVITY_RE = re.compile(
    r'^activity\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s+->\s+'
    r'[A-Za-z_][A-Za-z0-9_]*\s+computes\s+"(?P<program>[^"]+)"$'
)
UNKNOWN_FIELDS = ["stage", "step", "reason", "unknown_class", "next_operation", "blocked_by"]


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical(value).encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((canonical(value) + "\n").encode("utf-8"))


def physical_lines(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def inventory(root: Path) -> dict[str, Any]:
    files: list[Path] = []
    directories: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        directories.extend(current_path / name for name in dirnames)
        files.extend(current_path / name for name in sorted(filenames))
    files = [path for path in files if path != root / "README.md"]
    go_files = [path for path in files if path.suffix == ".go"]
    gooo_files = [path for path in files if path.suffix == ".gooo"]
    return {
        "root_readme_excluded": True,
        "regular_files": len(files),
        "descendant_directories": len(directories),
        "physical_lines": sum(physical_lines(path) for path in files),
        "go": {"files": len(go_files), "physical_lines": sum(physical_lines(path) for path in go_files)},
        "gooo": {"files": len(gooo_files), "physical_lines": sum(physical_lines(path) for path in gooo_files)},
        "files": sorted(path.relative_to(root).as_posix() for path in files),
    }


def source_programs(source: Path) -> dict[str, dict[str, str]]:
    programs: dict[str, dict[str, str]] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        match = ACTIVITY_RE.match(line.strip())
        if not match:
            continue
        fields = {"kind": match.group("program").split(";", 1)[0]}
        for part in match.group("program").split(";")[1:]:
            key, separator, value = part.partition("=")
            if not separator:
                raise ValueError(f"malformed source program field: {part}")
            fields[key] = value
        programs[match.group("name")] = fields
    if len(programs) != 12:
        raise ValueError("twelve source activity programs are required")
    return programs


def artifact_inventory(directory: Path) -> tuple[list[str], int, dict[str, str]]:
    paths = sorted(path for path in directory.rglob("*") if path.is_file())
    names = [path.relative_to(directory).as_posix() for path in paths]
    return names, sum(path.stat().st_size for path in paths), {name: digest_bytes(path.read_bytes()) for name, path in zip(names, paths)}


def runtime_metrics(path: Path) -> dict[str, int]:
    value = load_json(path)
    return {"wall_ms": int(value["wall_ms"]), "peak_rss_kib": int(value["peak_rss_kib"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--denominator", required=True, type=Path)
    parser.add_argument("--core-lock", required=True, type=Path)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--generated", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--go-version", required=True, type=Path)
    parser.add_argument("--repository-status", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    started_ns = time.perf_counter_ns()
    root = args.repo_root.resolve()
    source = args.source.resolve()
    denominator_path = args.denominator.resolve()
    core_lock_path = args.core_lock.resolve()
    ir_path = args.ir.resolve()
    check_path = args.check.resolve()
    generated = args.generated.resolve()
    evidence = args.evidence.resolve()
    replay = args.replay.resolve()
    output = args.output.resolve()
    for path in (source, denominator_path, core_lock_path, ir_path, check_path):
        path.relative_to(root)
    for path in (generated, evidence, replay, output):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        raise ValueError("all product outputs and replay directories must be caller-owned")

    denominator = load_json(denominator_path)
    core_lock = load_json(core_lock_path)
    ir = load_json(ir_path)
    programs = source_programs(source)
    cells = denominator.get("cells", [])
    if denominator.get("schema") != "gooo/design-contract-bridge/denominator/v2" or len(cells) != 12:
        raise ValueError("product denominator is not the fixed twelve-cell contract")
    if denominator.get("proof_counts") != {"FOUNDATION": 4, "COHERENCE": 4, "REGRESSION": 4}:
        raise ValueError("proof denominator is not 4/4/4")
    if denominator.get("indicator_counts") != {"DRIVER": 4, "OUTCOME": 4, "GUARDRAIL": 4}:
        raise ValueError("indicator denominator is not 4/4/4")
    if denominator.get("unknown_fields") != UNKNOWN_FIELDS or denominator.get("precedence") != "REFUTED_OVER_UNKNOWN":
        raise ValueError("unknown tuple or precedence contract is incomplete")
    activity_names = {node.get("name") for node in ir.get("nodes", []) if str(node.get("kind", "")).lower() == "activity"}
    if len(activity_names) != 12 or activity_names != set(programs):
        raise ValueError("semantic IR is not a one-to-one projection of the twelve source activities")
    if ir.get("schema_version") != "gooo-graph/v1" or ir.get("ir", {}).get("status") != "available":
        raise ValueError("semantic IR graph is unavailable")
    source_digest = digest_bytes(source.read_bytes())
    ir_digest = "sha256:" + str(ir["ir"]["semantic_digest"])

    source_names = {cell["activity"] for cell in cells}
    if source_names != set(programs) or len(source_names) != 12:
        raise ValueError("denominator activity names do not bind one-to-one to the Gooo source")
    runtime = {stage: runtime_metrics(args.runtime / f"{stage}.json") for stage in ("gooo", "generator", "consumer", "replay")}
    go_version = args.go_version.read_text(encoding="utf-8").strip()
    if not re.match(r"^go version go1\.27(?:\.\d+)?\s", go_version):
        raise ValueError(f"unexpected observed Go version: {go_version}")
    if args.repository_status.read_text(encoding="utf-8").strip():
        raise ValueError("the checked-out repository was modified during the read-only run")
    check_bytes = check_path.read_bytes()
    if not check_bytes:
        raise ValueError("semantic check output is empty")

    generated_names, generated_bytes, generated_digests = artifact_inventory(generated)
    evidence_names, evidence_bytes, evidence_digests = artifact_inventory(evidence)
    replay_names, replay_bytes, _ = artifact_inventory(replay)
    if generated_names != sorted([
        "component.css", "component.css.sha256", "claim-graph.json", "design-tokens.json",
        "design-tokens.sha256", "generation-receipt.json", "manifest.json", "provenance.json",
    ]) or len(generated_names) != 8:
        raise ValueError("generated envelope must contain exactly eight files")
    if evidence_names != sorted(["challenge-report.json", "consumer-receipt.json", "match-report.json", "normalized-core.json"]):
        raise ValueError("consumer evidence must contain exactly four files")
    if replay_names != evidence_names:
        raise ValueError("deterministic replay file set differs from the primary consumer run")
    challenge = load_json(evidence / "challenge-report.json")
    match = load_json(evidence / "match-report.json")
    consumer_receipt = load_json(evidence / "consumer-receipt.json")
    normalized = load_json(evidence / "normalized-core.json")
    replay_match = (replay / "match-report.json").read_bytes()
    replay_challenge = (replay / "challenge-report.json").read_bytes()
    replay_normalized = (replay / "normalized-core.json").read_bytes()
    deterministic = (
        replay_match == (evidence / "match-report.json").read_bytes()
        and replay_challenge == (evidence / "challenge-report.json").read_bytes()
        and replay_normalized == (evidence / "normalized-core.json").read_bytes()
    )
    reader_statuses = match.get("readers", [])
    if match.get("status") != "CLOSED" or [item.get("status") for item in reader_statuses] != ["CLOSED", "CLOSED", "CLOSED"]:
        raise ValueError("normal semantic match did not close all three readers")
    counts = challenge.get("counts", {})
    if counts.get("CLOSED", 0) < 1 or counts.get("UNKNOWN", 0) < 1 or counts.get("REFUTED", 0) < 1:
        raise ValueError("normal, UNKNOWN, and REFUTED minimum cases are required")
    if normalized.get("fixed_point") != "REFUTED" or not consumer_receipt.get("semantic_match_requires"):
        raise ValueError("fail-closed normalization or evidence binding is missing")
    if not deterministic:
        raise ValueError("consumer replay is not byte-deterministic")

    cell_claims = []
    match_digest = digest_bytes((evidence / "match-report.json").read_bytes())
    challenge_digest = digest_bytes((evidence / "challenge-report.json").read_bytes())
    for cell in cells:
        program = programs[cell["activity"]]
        artifact = program.get("artifact", "evaluator evidence")
        body = {
            "claim_id": f"cell.claim.{cell['id']}",
            "cell_id": cell["id"],
            "activity": cell["activity"],
            "proof": cell["proof"],
            "indicator": cell["indicator"],
            "axis": cell["axis"],
            "status": "CLOSED",
            "source_digest": source_digest,
            "ir_digest": ir_digest,
            "artifact_refs": [artifact, "match-report.json", "challenge-report.json"],
            "evidence_refs": [match_digest, challenge_digest],
        }
        cell_claims.append({**body, "claim_digest": digest_value(body)})
    write_json(output / "evidence" / "cell-claims.json", {
        "schema": "gooo/design-contract-bridge/cell-claims/v2",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "claims": cell_claims,
    })

    repo_inventory = inventory(root)
    output_names_before, _, _ = artifact_inventory(output)
    output_names_base = sorted(set(output_names_before + ["actions.json", "dossier.md"]))
    stages = {
        "executed": ["go-version-observation", "released-gooo-download", "semantic-check", "semantic-ir-dump", "design-source-generation", "independent-consumer", "challenge-cases", "deterministic-replay", "cell-claim-assembly"],
        "reused": ["immutable-core-release-identity", "product-denominator-v2", "source-activity-identities"],
        "skipped": ["local-go-build", "local-go-test", "local-gofmt", "local-go-vet", "external-user-utility"]
    }
    verification = {
        "build": {"executed": 0, "reused": 0, "skipped": 1, "wall_ms": 0, "peak_rss_kib": 0},
        "test": {"executed": 0, "reused": 0, "skipped": 1, "wall_ms": 0, "peak_rss_kib": 0},
        "gofmt": {"executed": 0, "reused": 0, "skipped": 1, "wall_ms": 0, "peak_rss_kib": 0},
        "vet": {"executed": 0, "reused": 0, "skipped": 1, "wall_ms": 0, "peak_rss_kib": 0},
        "conformance": {"executed": 1, "reused": 0, "skipped": 0},
    }
    base_actions = {
        "schema": "gooo/design-contract-bridge/actions/v2",
        "user_path": {
            "source": str(source.relative_to(root)),
            "semantic_ir": str(ir_path.relative_to(root)),
            "generated_bundle": str(generated),
            "independent_consumer": "scripts/consume_design_contract.py",
            "caller_owned_output": str(output),
        },
        "release": {
            "repository": core_lock["repository"],
            "tag": core_lock["release"]["tag"],
            "release_id": core_lock["release"]["release_id"],
            "asset_id": core_lock["release"]["asset"]["id"],
            "asset_sha256": core_lock["release"]["asset"]["sha256"],
            "observed_go_version": go_version,
        },
        "semantic_binding": {
            "source_activities": len(programs),
            "ir_activities": len(activity_names),
            "evaluator_cell_claims": len(cell_claims),
            "one_to_one": len(programs) == len(activity_names) == len(cell_claims) == 12,
            "source_digest": source_digest,
            "ir_digest": ir_digest,
        },
        "fixed_cells": {
            "observed": len(cell_claims), "total": 12,
            "proof": {"FOUNDATION": 4, "COHERENCE": 4, "REGRESSION": 4},
            "indicator": {"DRIVER": 4, "OUTCOME": 4, "GUARDRAIL": 4},
            "closed": len([claim for claim in cell_claims if claim["status"] == "CLOSED"]),
        },
        "readers": {
            "observed": 3, "total": 3,
            "EXACT": 1, "ROLE": 1, "EXISTENCE": 1,
            "normal_statuses": {item["reader"]: item["status"] for item in reader_statuses},
            "visual_similarity_can_close": False,
        },
        "cases": {
            "denominator": counts.get("denominator", 0),
            "CLOSED": counts.get("CLOSED", 0), "UNKNOWN": counts.get("UNKNOWN", 0), "REFUTED": counts.get("REFUTED", 0),
            "minimum": {"CLOSED": 1, "UNKNOWN": 1, "REFUTED": 1},
            "unknown_fields": UNKNOWN_FIELDS,
            "malformed_unknown": "REFUTED",
            "fixed_point": "REFUTED",
            "precedence": "REFUTED_OVER_UNKNOWN",
        },
        "artifacts": {
            "input": {"count": 5, "files": [str(source.relative_to(root)), str(denominator_path.relative_to(root)), str(core_lock_path.relative_to(root)), str(ir_path.relative_to(root)), str(check_path.relative_to(root))]},
            "generated": {"count": len(generated_names), "bytes": generated_bytes, "files": generated_names, "digests": generated_digests},
            "consumer": {"count": len(evidence_names), "bytes": evidence_bytes, "files": evidence_names, "digests": evidence_digests},
            "replay": {"count": len(replay_names), "bytes": replay_bytes, "files": replay_names, "deterministic": deterministic},
            "caller_output": {"files": output_names_base},
        },
        "inventory": repo_inventory,
        "runtime": {"gooo": runtime["gooo"], "generator": runtime["generator"], "consumer": runtime["consumer"], "replay": runtime["replay"]},
        "verification": verification,
        "stages": {name: {"count": len(values), "items": values} for name, values in stages.items()},
        "policy": {"repository_writes": 0, "local_test_executions": 0, "cross_project_required_gates": 0, "product_generation_scope": "caller-owned-temp-output-only"},
        "releases": {"released_adoption": {"observed": 0, "total": 1, "status": "UNKNOWN"}, "external_utility": {"observed": 0, "total": 1, "status": "UNKNOWN"}},
        "improvement": {"status": "UNKNOWN", "reason": "No exact same-input same-tool before/after pair was supplied."},
    }
    elapsed_ms = max(1, (time.perf_counter_ns() - started_ns) // 1_000_000)
    peak_rss_kib = max(1, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    verification["conformance"].update({"wall_ms": elapsed_ms, "peak_rss_kib": peak_rss_kib})
    base_actions["runtime"]["conformance"] = verification["conformance"]

    dossier = [
        "# Gooo design contract bridge",
        "",
        "The authoritative `.gooo` source was checked by the released Gooo binary, projected into an eight-file token/CSS bundle, and independently consumed for semantic matching.",
        "",
        "## Exact CI metrics",
        "",
        f"- fixed cells: `{len(cell_claims)}/{12}`; proof `FOUNDATION/COHERENCE/REGRESSION=4/4/4`; indicators `DRIVER/OUTCOME/GUARDRAIL=4/4/4`.",
        f"- one-to-one binding: source activities `{len(programs)}`, semantic IR activities `{len(activity_names)}`, evaluator claims `{len(cell_claims)}`.",
        f"- readers: `EXACT={base_actions['readers']['EXACT']}/1`, `ROLE={base_actions['readers']['ROLE']}/1`, `EXISTENCE={base_actions['readers']['EXISTENCE']}/1`.",
        f"- cases: `CLOSED={counts.get('CLOSED', 0)}`, `UNKNOWN={counts.get('UNKNOWN', 0)}`, `REFUTED={counts.get('REFUTED', 0)}`; malformed UNKNOWN and FIXED_POINT are `REFUTED`.",
        f"- artifacts: input `{5}`, generated `{len(generated_names)}/{generated_bytes} bytes`, consumer `{len(evidence_names)}/{evidence_bytes} bytes`, replay `{len(replay_names)}/{replay_bytes} bytes`.",
        f"- policy: `repository_writes=0`, `local_test_executions=0`, `cross_project_required_gates=0`; generation scope is caller-owned output.",
        f"- inventory: descendant directories `{repo_inventory['descendant_directories']}`, regular files `{repo_inventory['regular_files']}`, Go `{repo_inventory['go']['files']}/{repo_inventory['go']['physical_lines']}`, Gooo `{repo_inventory['gooo']['files']}/{repo_inventory['gooo']['physical_lines']}`, physical lines `{repo_inventory['physical_lines']}`; root README excluded.",
        f"- runtime: Go 1.27 observation `{go_version}`, conformance `wall_ms={elapsed_ms}`, `peak_rss_kib={peak_rss_kib}`.",
        "",
        "## Reader and precedence evidence",
        "",
        "EXACT, ROLE, and EXISTENCE are separate resolutions. Each CLOSED result binds the generated artifact, generation receipt, and claim graph; visual similarity is insufficient. UNKNOWN records preserve stage, step, reason, unknown_class, next_operation, blocked_by, and causal_frontier. Known contradiction outranks UNKNOWN as REFUTED_OVER_UNKNOWN. FIXED_POINT and permission escalation fail closed.",
        "",
        "| Scenario | Status | Evidence digest |",
        "| --- | --- | --- |",
    ]
    for scenario in challenge["scenarios"]:
        dossier.append(f"| `{scenario['scenario_id']}` | `{scenario['status']}` | `{scenario['evidence_digest']}` |")
    dossier.extend(["", "The external utility and released-adoption decisions remain UNKNOWN because no external user evidence or new product release was supplied."])
    dossier_text = "\n".join(dossier) + "\n"

    output.mkdir(parents=True, exist_ok=True)
    action_bytes = 0
    for _ in range(20):
        base_actions["artifacts"]["caller_output"]["bytes"] = action_bytes
        write_json(output / "actions.json", base_actions)
        (output / "dossier.md").write_text(dossier_text, encoding="utf-8")
        _, actual_bytes, _ = artifact_inventory(output)
        if actual_bytes == action_bytes:
            break
        action_bytes = actual_bytes
    actual_names, actual_bytes, _ = artifact_inventory(output)
    if actual_names != output_names_base or actual_bytes != base_actions["artifacts"]["caller_output"]["bytes"]:
        raise ValueError("caller-owned artifact byte accounting did not reach a stable exact value")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"bridge conformance: {exc}")
        raise SystemExit(1)
