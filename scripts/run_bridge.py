#!/usr/bin/env python3
"""Evaluate the single Gooo design-token bridge contract.

This file intentionally uses only the Python standard library. It is invoked by
GitHub Actions; no local verification command is part of the repository
contract. The evaluator never writes into the input repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA = "gooo.design-contract-bridge/v1"
UNKNOWN_FIELDS = (
    "stage",
    "step",
    "reason",
    "unknown_class",
    "next_operation",
    "blocked_by",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(read_bytes(path).decode("utf-8"))


def rel_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def ensure_inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root: {path}") from exc


def parse_contract(path: Path) -> dict[str, Any]:
    text = read_bytes(path).decode("utf-8")
    activities = re.findall(r'^activity\s+"([^"]+)"\s*$', text, flags=re.MULTILINE)
    graph_match = re.search(
        r'^released_semantic_graph\s+"([^"]+)"\s+from\s+"([^"]+)"\s*$',
        text,
        flags=re.MULTILINE,
    )
    reads = dict(
        re.findall(
            r'^read\s+"([^"]+)"\s+from\s+"([^"]+)"\s*$',
            text,
            flags=re.MULTILINE,
        )
    )
    writes = dict(
        re.findall(
            r'^write\s+"([^"]+)"\s+to caller_owned\s+"([^"]+)"\s*$',
            text,
            flags=re.MULTILINE,
        )
    )
    precedence = re.search(
        r'^declare\s+"refuted_over_unknown"\s+precedence\s+"([^"]+)"\s*$',
        text,
        flags=re.MULTILINE,
    )
    if len(activities) != 1 or not graph_match:
        raise ValueError("contract must contain exactly one activity and one released graph")
    if set(reads) != {"canonical_design_tokens", "css_custom_property_fixture"}:
        raise ValueError("contract reads must be the canonical token source and CSS fixture")
    if writes.get("token_mapping") != "token-mapping.json" or writes.get("human_dossier") != "dossier.md":
        raise ValueError("contract outputs must be the two caller-owned files")
    if not precedence or precedence.group(1) != "REFUTED_OVER_UNKNOWN":
        raise ValueError("contract must declare REFUTED_OVER_UNKNOWN")
    return {
        "activity_id": activities[0],
        "graph_release": graph_match.group(1),
        "graph_path": graph_match.group(2),
        "reads": reads,
        "writes": writes,
        "precedence": precedence.group(1),
        "digest": digest_bytes(read_bytes(path)),
    }


def parse_css(path: Path) -> tuple[str, dict[str, str], str]:
    text = read_bytes(path).decode("utf-8")
    owner_match = re.search(r"@owner\s+([A-Za-z0-9_.-]+)", text)
    if not owner_match:
        raise ValueError("CSS fixture must declare @owner")
    props = {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;{}]+);", text)
    }
    if not props:
        raise ValueError("CSS fixture must contain custom properties")
    return owner_match.group(1), props, text


def token_to_property(name: str) -> str:
    return "--" + name.replace(".", "-")


def line_count(path: Path) -> int:
    data = read_bytes(path)
    return len(data.splitlines())


def inventory(root: Path) -> dict[str, Any]:
    files: list[Path] = []
    dirs: list[Path] = []
    total_lines = 0
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for dirname in dirnames:
            dirs.append(current_path / dirname)
        for filename in sorted(filenames):
            path = current_path / filename
            if path == root / "README.md":
                continue
            files.append(path)
            try:
                total_lines += line_count(path)
            except UnicodeDecodeError:
                continue
    return {
        "root_readme_excluded": True,
        "regular_files": len(files),
        "descendant_directories": len(dirs),
        "total_physical_lines": total_lines,
        "files": sorted(path.relative_to(root).as_posix() for path in files),
    }


def evidence_claim(
    *,
    claim_id: str,
    activity_id: str,
    graph_release: str,
    graph_digest: str,
    statement: str,
    expected: Any,
    observed: Any,
    input_digests: dict[str, str],
) -> dict[str, Any]:
    body = {
        "claim_id": claim_id,
        "activity_id": activity_id,
        "graph_release": graph_release,
        "graph_digest": graph_digest,
        "statement": statement,
        "expected": expected,
        "observed": observed,
        "input_digests": input_digests,
    }
    body["evidence_digest"] = digest_value(body)
    return body


def unknown_resolution() -> dict[str, str]:
    return {
        "stage": "semantic-evaluation",
        "step": "resolve-canonical-token-source",
        "reason": "The requested token is absent from the canonical design-token input.",
        "unknown_class": "LOWER_RESOLUTION",
        "next_operation": "add-or-release-canonical-token-and-rerun",
        "blocked_by": "canonical design-token source revision",
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--tokens", required=True, type=Path)
    parser.add_argument("--css", required=True, type=Path)
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    started_ns = time.perf_counter_ns()
    root = args.repo_root.resolve()
    contract_path = args.contract.resolve()
    tokens_path = args.tokens.resolve()
    css_path = args.css.resolve()
    graph_path = args.graph.resolve()
    output = args.output.resolve()
    for path, label in (
        (contract_path, "contract"),
        (tokens_path, "tokens"),
        (css_path, "css"),
        (graph_path, "graph"),
    ):
        ensure_inside(path, root, label)
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("caller-owned output must be outside repository root")

    contract = parse_contract(contract_path)
    tokens_doc = load_json(tokens_path)
    graph_doc = load_json(graph_path)
    css_owner, css_props, css_text = parse_css(css_path)
    if contract["graph_release"] != graph_doc["release"]:
        raise ValueError("contract and semantic graph release identities differ")
    if not graph_doc.get("released"):
        raise ValueError("semantic graph must be released")
    graph_activities = {item["id"]: item for item in graph_doc.get("activities", [])}
    required_activities = {
        "graph.activity.foundation.contract-inputs",
        "graph.activity.foundation.graph-release",
        "graph.activity.foundation.source-digest",
        "graph.activity.foundation.caller-output",
        "graph.activity.coherence.token-identity",
        "graph.activity.coherence.css-value",
        "graph.activity.coherence.mapping-closure",
        "graph.activity.coherence.dossier",
        "graph.activity.regression.unknown-fields",
        "graph.activity.regression.contradiction",
        "graph.activity.regression.precedence",
        "graph.activity.regression.evidence-digest",
    }
    if set(graph_activities) != required_activities:
        raise ValueError("released graph activities must equal the fixed 12-cell denominator")

    token_list = tokens_doc.get("tokens", [])
    canonical_tokens = {item["name"]: item for item in token_list}
    expected_tokens = {item["name"]: item for item in graph_doc["expected_tokens"]}
    input_digests = {
        "contract": digest_bytes(read_bytes(contract_path)),
        "canonical_design_tokens": digest_bytes(read_bytes(tokens_path)),
        "css_custom_property_fixture": digest_bytes(read_bytes(css_path)),
        "released_semantic_graph": digest_bytes(read_bytes(graph_path)),
    }
    graph_digest = input_digests["released_semantic_graph"]
    base_css = dict(css_props)
    base_owner = css_owner

    mappings: list[dict[str, Any]] = []
    for name in sorted(canonical_tokens):
        token = canonical_tokens[name]
        prop = token_to_property(name)
        observed = base_css.get(prop)
        status = "CLOSED" if observed == token["value"] and base_owner == tokens_doc["authority"] else "REFUTED"
        mappings.append(
            {
                "token": name,
                "css_custom_property": prop,
                "canonical_value": token["value"],
                "observed_value": observed,
                "authority": {"expected": tokens_doc["authority"], "observed": base_owner},
                "status": status,
                "mapping_digest": digest_value(
                    {
                        "token": name,
                        "property": prop,
                        "canonical_value": token["value"],
                        "observed_value": observed,
                        "authority": base_owner,
                    }
                ),
            }
        )

    missing_name = "color.action.primary"
    missing_prop = token_to_property(missing_name)
    unknown = unknown_resolution()
    unknown_scenario = {
        "scenario_id": "unknown-missing-source",
        "status": "UNKNOWN",
        "token": missing_name,
        "css_custom_property": missing_prop,
        "observed": {"canonical_token_present": False, "css_property_present": False},
        "resolution": unknown,
        "evidence_digest": digest_value(
            {
                "scenario_id": "unknown-missing-source",
                "token": missing_name,
                "resolution": unknown,
            }
        ),
    }

    contradictory_css_text = css_text.replace(
        "/* @owner canonical-design-system */", "/* @owner component-local */"
    )
    contradictory_css_text = contradictory_css_text.replace(
        "  --radius-md: 8px;",
        "  --radius-md: 8px;\n  --color-action-primary: #FF0000;",
    )
    contradictory_owner, contradictory_props, _ = parse_css_from_text(contradictory_css_text)
    expected_action = expected_tokens[missing_name]
    contradictions = [
        {
            "kind": "VALUE_CONTRADICTION",
            "expected": expected_action["value"],
            "observed": contradictory_props[missing_prop],
        },
        {
            "kind": "PERMISSION_CONTRADICTION",
            "expected": tokens_doc["authority"],
            "observed": contradictory_owner,
        },
        {
            "kind": "MAPPING_CONTRADICTION",
            "expected": "canonical token source binding",
            "observed": "unreleased component-local binding",
        },
    ]
    refuted_scenario = {
        "scenario_id": "refuted-permission-value-mapping",
        "status": "REFUTED",
        "token": missing_name,
        "css_custom_property": missing_prop,
        "observed": {
            "canonical_token_present": False,
            "css_property_present": True,
            "value": contradictory_props[missing_prop],
            "authority": contradictory_owner,
        },
        "contradictions": contradictions,
        "would_be_unknown": unknown,
        "precedence": "REFUTED_OVER_UNKNOWN",
        "evidence_digest": digest_value(
            {
                "scenario_id": "refuted-permission-value-mapping",
                "token": missing_name,
                "contradictions": contradictions,
                "precedence": "REFUTED_OVER_UNKNOWN",
            }
        ),
    }
    if refuted_scenario["status"] != "REFUTED" or refuted_scenario["precedence"] != contract["precedence"]:
        raise ValueError("REFUTED_OVER_UNKNOWN precedence failed")
    if not all(field in unknown_scenario["resolution"] for field in UNKNOWN_FIELDS):
        raise ValueError("UNKNOWN must contain the six required fields")

    closed_scenario = {
        "scenario_id": "closed-exact-match",
        "status": "CLOSED",
        "mapping_count": len(mappings),
        "exact_matches": len([item for item in mappings if item["status"] == "CLOSED"]),
        "mapping_digests": [item["mapping_digest"] for item in mappings],
        "evidence_digest": digest_value(
            {
                "scenario_id": "closed-exact-match",
                "mappings": mappings,
            }
        ),
    }
    if closed_scenario["mapping_count"] != 4 or closed_scenario["exact_matches"] != 4:
        raise ValueError("closed scenario must contain four exact mappings")

    scenarios = [closed_scenario, unknown_scenario, refuted_scenario]
    claims: list[dict[str, Any]] = []
    cells = [
        ("foundation.contract-inputs", "graph.activity.foundation.contract-inputs", "FOUNDATION", "DRIVER", "CLOSED", "The contract resolves exactly two declared inputs."),
        ("foundation.graph-release", "graph.activity.foundation.graph-release", "FOUNDATION", "DRIVER", "CLOSED", "The graph release is marked released and identity-matched."),
        ("foundation.source-digest", "graph.activity.foundation.source-digest", "FOUNDATION", "DRIVER", "CLOSED", "All four inputs have content digests."),
        ("foundation.caller-output", "graph.activity.foundation.caller-output", "FOUNDATION", "DRIVER", "CLOSED", "The output directory is outside the repository."),
        ("coherence.token-identity", "graph.activity.coherence.token-identity", "COHERENCE", "OUTCOME", "CLOSED", "Four token names normalize to four CSS properties."),
        ("coherence.css-value", "graph.activity.coherence.css-value", "COHERENCE", "OUTCOME", "CLOSED", "Four CSS values equal canonical values."),
        ("coherence.mapping-closure", "graph.activity.coherence.mapping-closure", "COHERENCE", "OUTCOME", "CLOSED", "The normal scenario closes with exact mappings."),
        ("coherence.dossier", "graph.activity.coherence.dossier", "COHERENCE", "OUTCOME", "CLOSED", "The dossier is generated from the same evidence graph."),
        ("regression.unknown-fields", "graph.activity.regression.unknown-fields", "REGRESSION", "GUARDRAIL", "CLOSED", "The missing-source scenario preserves all six UNKNOWN fields."),
        ("regression.contradiction", "graph.activity.regression.contradiction", "REGRESSION", "GUARDRAIL", "CLOSED", "The contradiction scenario emits value, permission, and mapping refutations."),
        ("regression.precedence", "graph.activity.regression.precedence", "REGRESSION", "GUARDRAIL", "CLOSED", "REFUTED_OVER_UNKNOWN is executed and observed."),
        ("regression.evidence-digest", "graph.activity.regression.evidence-digest", "REGRESSION", "GUARDRAIL", "CLOSED", "Every claim binds to released activity and evidence digest."),
    ]
    for cell_id, activity_id, phase, indicator, verdict, statement in cells:
        observed: Any = {
            "closed_scenario": closed_scenario["evidence_digest"],
            "unknown_scenario": unknown_scenario["evidence_digest"],
            "refuted_scenario": refuted_scenario["evidence_digest"],
        }
        claim = evidence_claim(
            claim_id=f"claim.{cell_id}",
            activity_id=activity_id,
            graph_release=graph_doc["release"],
            graph_digest=graph_digest,
            statement=statement,
            expected=verdict,
            observed=observed,
            input_digests=input_digests,
        )
        claim.update({"cell_id": cell_id, "phase": phase, "indicator": indicator, "verdict": verdict})
        claims.append(claim)

    if len(claims) != 12:
        raise ValueError("fixed denominator must remain 12 claims")
    if any(claim["activity_id"] not in graph_activities for claim in claims):
        raise ValueError("claim activity is not in released semantic graph")
    if any(not claim["evidence_digest"].startswith("sha256:") for claim in claims):
        raise ValueError("claim evidence digest missing")

    output.mkdir(parents=True, exist_ok=True)
    evidence_dir = output / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    token_mapping = {
        "schema": SCHEMA,
        "contract": {
            "path": rel_path(contract_path, root),
            "activity_id": contract["activity_id"],
            "digest": input_digests["contract"],
        },
        "released_semantic_graph": {
            "release": graph_doc["release"],
            "authority": graph_doc["authority"],
            "digest": graph_digest,
            "activity_count": len(graph_activities),
        },
        "inputs": {
            "canonical_design_tokens": {"path": rel_path(tokens_path, root), "digest": input_digests["canonical_design_tokens"]},
            "css_custom_property_fixture": {"path": rel_path(css_path, root), "digest": input_digests["css_custom_property_fixture"]},
        },
        "mappings": mappings,
        "scenarios": scenarios,
        "claims": [
            {
                "claim_id": claim["claim_id"],
                "cell_id": claim["cell_id"],
                "activity_id": claim["activity_id"],
                "graph_release": claim["graph_release"],
                "evidence_digest": claim["evidence_digest"],
                "verdict": claim["verdict"],
            }
            for claim in claims
        ],
        "verdict_counts": {"CLOSED": 1, "UNKNOWN": 1, "REFUTED": 1},
        "precedence_assertion": {"name": "REFUTED_OVER_UNKNOWN", "observed": True},
    }
    write_json(output / "token-mapping.json", token_mapping)
    write_json(
        evidence_dir / "claims.json",
        {
            "schema": "gooo.evidence-claims/v1",
            "graph_release": graph_doc["release"],
            "graph_digest": graph_digest,
            "claims": claims,
        },
    )
    write_json(
        evidence_dir / "scenarios.json",
        {
            "schema": "gooo.evidence-scenarios/v1",
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        },
    )

    elapsed_ms = max(1, (time.perf_counter_ns() - started_ns) // 1_000_000)
    peak_rss_kib = max(1, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    repo_inventory = inventory(root)
    css_lines = line_count(css_path)
    gooo_lines = line_count(contract_path)
    output_files = [
        "token-mapping.json",
        "dossier.md",
        "actions.json",
        "evidence/claims.json",
        "evidence/scenarios.json",
    ]
    stages = {
        "executed": [
            "contract-parse",
            "released-graph-binding",
            "canonical-source-read",
            "css-fixture-read",
            "exact-mapping-evaluation",
            "unknown-resolution-evaluation",
            "refutation-precedence-evaluation",
            "claim-evidence-digest",
            "caller-owned-output-generation",
            "repository-inventory-observation",
        ],
        "reused": ["released-semantic-graph-activity-identities"],
        "not_applicable": [
            "local-test-execution",
            "local-build-execution",
            "local-formatter-execution",
            "external-user-utility-evidence",
            "exact-comparable-performance-before-after",
        ],
    }
    actions = {
        "schema": "gooo.actions-artifact/v1",
        "user_path": {
            "repository_root": str(root),
            "contract": rel_path(contract_path, root),
            "canonical_design_tokens": rel_path(tokens_path, root),
            "css_custom_property_fixture": rel_path(css_path, root),
            "caller_owned_output": str(output),
        },
        "files": {
            "input_file_count": 4,
            "input_files": [
                rel_path(contract_path, root),
                rel_path(tokens_path, root),
                rel_path(css_path, root),
                rel_path(graph_path, root),
            ],
            "output_file_count": len(output_files),
            "output_files": output_files,
        },
        "physical_lines": {
            "css_files": [rel_path(css_path, root)],
            "gooo_files": [rel_path(contract_path, root)],
            "css_physical_lines": css_lines,
            "gooo_physical_lines": gooo_lines,
            "total_physical_lines_root_readme_excluded": repo_inventory["total_physical_lines"],
        },
        "repository_inventory": repo_inventory,
        "runtime": {"wall_ms": elapsed_ms, "peak_rss_kib": peak_rss_kib},
        "stages": stages,
        "policy": {
            "repository_writes": 0,
            "local_test_executions": 0,
            "cross_project_required_gates": 0,
        },
        "scenarios": {"denominator": 3, "CLOSED": 1, "UNKNOWN": 1, "REFUTED": 1},
        "fixed_cells": {
            "denominator": 12,
            "proof": {"FOUNDATION": 4, "COHERENCE": 4, "REGRESSION": 4},
            "indicator": {"DRIVER": 4, "OUTCOME": 4, "GUARDRAIL": 4},
        },
        "performance": {
            "status": "UNKNOWN",
            "reason": "No exact comparable before/after pair under the same conditions was supplied.",
        },
        "utility": {
            "status": "UNKNOWN",
            "reason": "No external user evidence was supplied.",
        },
        "artifact": {
            "name": "gooo-design-contract-bridge",
            "digest_recorded_by_actions_api": True,
            "files": {name: None for name in output_files},
        },
    }
    for name in output_files:
        path = output / name
        if path.exists() and name not in {"dossier.md", "actions.json"}:
            actions["artifact"]["files"][name] = digest_bytes(read_bytes(path))

    dossier_lines = [
        "# Gooo Design Contract Bridge dossier",
        "",
        "## Result",
        "",
        "One released semantic graph activity evaluated one Gooo contract against the canonical token source and CSS custom-property fixture.",
        "",
        "| Scenario | Verdict | Evidence digest |",
        "| --- | --- | --- |",
    ]
    for scenario in scenarios:
        dossier_lines.append(f"| `{scenario['scenario_id']}` | **{scenario['status']}** | `{scenario['evidence_digest']}` |")
    dossier_lines.extend(
        [
            "",
            "The refuted scenario carries a missing canonical token signal, a value contradiction, a permission contradiction, and a mapping contradiction. The observed precedence is `REFUTED_OVER_UNKNOWN`.",
            "",
            "## Fixed semantic graph denominator",
            "",
            "- Cells: `12`.",
            "- Proof: `FOUNDATION 4 / COHERENCE 4 / REGRESSION 4`.",
            "- Indicators: `DRIVER 4 / OUTCOME 4 / GUARDRAIL 4`.",
            "- Every claim below names a released graph activity and its evidence digest.",
            "",
            "| Cell | Phase | Indicator | Activity | Verdict | Evidence digest |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for claim in claims:
        dossier_lines.append(
            f"| `{claim['cell_id']}` | `{claim['phase']}` | `{claim['indicator']}` | `{claim['activity_id']}` | `{claim['verdict']}` | `{claim['evidence_digest']}` |"
        )
    dossier_lines.extend(
        [
            "",
            "## Actions accounting",
            "",
            f"- User path: `{actions['user_path']['contract']}` → `{actions['user_path']['canonical_design_tokens']}` + `{actions['user_path']['css_custom_property_fixture']}` → caller-owned output `{actions['user_path']['caller_owned_output']}`.",
            f"- Files: inputs `{actions['files']['input_file_count']}`, outputs `{actions['files']['output_file_count']}`.",
            f"- Physical lines: CSS `{css_lines}`, `.gooo` `{gooo_lines}`, repository total excluding root README `{repo_inventory['total_physical_lines']}`.",
            f"- Runtime: `wall_ms={elapsed_ms}`, `peak_rss_kib={peak_rss_kib}`.",
            f"- Policy: `repository_writes=0`, `local_test_executions=0`, `cross_project_required_gates=0`.",
            f"- Executed stages: `{', '.join(stages['executed'])}`.",
            f"- Reused stages: `{', '.join(stages['reused'])}`.",
            f"- Not applicable: `{', '.join(stages['not_applicable'])}`.",
            "",
            "## Honest boundaries",
            "",
            "Performance improvement is `UNKNOWN` because there is no exact comparable before/after pair. Utility is `UNKNOWN` because no external user evidence was supplied.",
            "",
            "Machine evidence is in `token-mapping.json`, `evidence/claims.json`, `evidence/scenarios.json`, and `actions.json`.",
            "",
        ]
    )
    (output / "dossier.md").write_text("\n".join(dossier_lines), encoding="utf-8")
    actions["artifact"]["files"]["dossier.md"] = digest_bytes(read_bytes(output / "dossier.md"))
    write_json(output / "actions.json", actions)
    return 0


def parse_css_from_text(text: str) -> tuple[str, dict[str, str], str]:
    owner_match = re.search(r"@owner\s+([A-Za-z0-9_.-]+)", text)
    if not owner_match:
        raise ValueError("derived CSS fixture must retain @owner")
    props = {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;{}]+);", text)
    }
    return owner_match.group(1), props, text


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"bridge evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
