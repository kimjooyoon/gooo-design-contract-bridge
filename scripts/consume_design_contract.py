#!/usr/bin/env python3
"""Independently consume a generated design bundle and resolve its meaning."""

from __future__ import annotations

import argparse
import copy
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
UNKNOWN_FIELDS = ["stage", "step", "reason", "unknown_class", "next_operation", "blocked_by"]
ACTIVITY_RE = re.compile(
    r'^activity\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s+->\s+'
    r'[A-Za-z_][A-Za-z0-9_]*\s+computes\s+"(?P<program>[^"]+)"$'
)
CSS_RE = re.compile(r"(?P<property>--[a-z0-9-]+)\s*:\s*(?P<value>[^;{}]+);")


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


def parse_program(program: str) -> dict[str, str]:
    parts = program.split(";")
    result = {"kind": parts[0]}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator or not key or not value or key in result:
            raise ValueError(f"malformed Gooo program: {program}")
        result[key] = value
    return result


def source_activity_records(source: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        match = ACTIVITY_RE.match(line.strip())
        if not match:
            continue
        meaning = parse_program(match.group("program"))
        record = {
            "activity": match.group("name"),
            "line": line_number,
            "program": match.group("program"),
            "program_digest": digest_bytes(match.group("program").encode("utf-8")),
            "meaning": meaning,
        }
        records.append(record)
    if len(records) != 12 or len({record["activity"] for record in records}) != 12:
        raise ValueError("consumer requires twelve unique computed Gooo activities")
    return records


def source_intents(source: Path) -> tuple[str, list[dict[str, Any]]]:
    source_bytes = source.read_bytes()
    records = source_activity_records(source)
    intents = []
    for record in records:
        meaning = record["meaning"]
        if meaning.get("kind") != "design.token:v2":
            continue
        intents.append({
            "name": meaning["name"],
            "value": meaning["value"],
            "property": meaning["property"],
            "role": meaning["role"],
            "claim_id": meaning["claim"],
            "source_activity": record["activity"],
            "source_line": record["line"],
            "source_program_digest": record["program_digest"],
        })
    if len(intents) != 4:
        raise ValueError("consumer requires four token intents from the Gooo source")
    return digest_bytes(source_bytes), sorted(intents, key=lambda item: item["name"])


def unknown_resolution(*, step: str, reason: str, unknown_class: str, next_operation: str, blocked_by: list[str], stage: str = "semantic-reader") -> dict[str, Any]:
    return {
        "stage": stage,
        "step": step,
        "reason": reason,
        "unknown_class": unknown_class,
        "next_operation": next_operation,
        "blocked_by": blocked_by,
        "causal_frontier": blocked_by,
    }


def descent_edge(*, source: str, target: str, resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": source,
        "to": target,
        **resolution,
    }


def parse_css(path: Path) -> dict[str, str]:
    return {match.group("property"): match.group("value").strip() for match in CSS_RE.finditer(path.read_text(encoding="utf-8"))}


def validate_bundle(
    bundle: Path,
    source_digest: str,
    ir_digest: str,
    intents: list[dict[str, Any]],
    activity_records: list[dict[str, Any]],
) -> dict[str, Any]:
    files = {name: bundle / name for name in BUNDLE_FILES}
    if any(not path.is_file() for path in files.values()):
        raise ValueError("generated bundle is missing one of its eight files")
    manifest = load_json(files["manifest.json"])
    tokens = load_json(files["design-tokens.json"])
    receipt = load_json(files["generation-receipt.json"])
    claims = load_json(files["claim-graph.json"])
    provenance = load_json(files["provenance.json"])
    if manifest.get("files") != BUNDLE_FILES or manifest.get("source_digest") != source_digest or manifest.get("ir_digest") != ir_digest:
        raise ValueError("generated manifest is not bound to the source and semantic IR")
    if manifest.get("activities") != [record["activity"] for record in activity_records]:
        raise ValueError("generated manifest activity order is not bound to the source")
    if tokens.get("source", {}).get("digest") != source_digest or tokens.get("semantic_ir", {}).get("digest") != ir_digest:
        raise ValueError("generated token artifact lost source or IR binding")
    if receipt.get("source_digest") != source_digest or receipt.get("ir_digest") != ir_digest:
        raise ValueError("generation receipt lost source or IR binding")
    if claims.get("source_digest") != source_digest or claims.get("ir_digest") != ir_digest:
        raise ValueError("claim graph lost source or IR binding")
    if provenance.get("source_digest") != source_digest or provenance.get("ir_digest") != ir_digest:
        raise ValueError("provenance lost source or IR binding")
    activity_binding_list = receipt.get("activity_bindings", [])
    if len(activity_binding_list) != len(activity_records):
        raise ValueError("generation receipt does not contain one binding for each source activity")
    activity_by_name = {record["activity"]: record for record in activity_records}
    binding_by_name: dict[str, dict[str, Any]] = {}
    for binding in activity_binding_list:
        source_activity = binding.get("source_activity", {})
        name = source_activity.get("name")
        if name not in activity_by_name or name in binding_by_name:
            raise ValueError("generation receipt activity bindings are not one-to-one")
        if source_activity.get("line") != activity_by_name[name]["line"] or source_activity.get("program_digest") != activity_by_name[name]["program_digest"]:
            raise ValueError(f"generation receipt source binding disagrees with source activity: {name}")
        semantic_activity = binding.get("semantic_ir_activity", {})
        if semantic_activity.get("name") != name or not semantic_activity.get("digest"):
            raise ValueError(f"generation receipt semantic IR binding is incomplete: {name}")
        binding_body = {key: value for key, value in binding.items() if key != "binding_digest"}
        if binding.get("binding_digest") != digest_value(binding_body):
            raise ValueError(f"generation receipt activity binding digest is stale: {name}")
        if not binding.get("generated_artifacts"):
            raise ValueError(f"generation receipt generated artifact binding is empty: {name}")
        binding_by_name[name] = binding
    if set(binding_by_name) != set(activity_by_name):
        raise ValueError("generation receipt activity binding set differs from source activities")
    if receipt.get("activity_binding_digest") != digest_value(activity_binding_list):
        raise ValueError("generation receipt activity binding digest is stale")
    for intent in intents:
        binding = binding_by_name[intent["source_activity"]]
        intent["semantic_ir_activity"] = binding["semantic_ir_activity"]
        intent["activity_binding_digest"] = binding["binding_digest"]
    observed_tokens = sorted(tokens.get("tokens", []), key=lambda item: item.get("name", ""))
    if observed_tokens != intents:
        raise ValueError("generated token artifact does not equal the independently parsed Gooo intents")
    claim_list = claims.get("claims", [])
    if len(claim_list) != len(intents) or len({claim.get("claim_id") for claim in claim_list}) != len(intents):
        raise ValueError("claim graph cardinality does not equal the four source intents")
    artifact_digests = {
        "design-tokens.json": digest_bytes(files["design-tokens.json"].read_bytes()),
        "component.css": digest_bytes(files["component.css"].read_bytes()),
        "claim-graph.json": digest_bytes(files["claim-graph.json"].read_bytes()),
    }
    receipt_digests = receipt.get("generated_artifact_digests", {})
    receipt_binding = all(receipt_digests.get(name) == digest for name, digest in artifact_digests.items())
    checksum_binding = all(
        files[name].read_text(encoding="utf-8").strip() == digest.removeprefix("sha256:") + "  " + target
        for name, digest, target in (
            ("design-tokens.sha256", artifact_digests["design-tokens.json"], "design-tokens.json"),
            ("component.css.sha256", artifact_digests["component.css"], "component.css"),
        )
    )
    claim_binding = all(
        claim.get("source_digest") == source_digest
        and claim.get("ir_digest") == ir_digest
        and claim.get("generated_artifacts") == ["design-tokens.json", "component.css", "generation-receipt.json", "claim-graph.json"]
        and claim.get("activity_binding_digest") == binding_by_name.get(claim.get("activity"), {}).get("binding_digest")
        for claim in claim_list
    )
    css_props = parse_css(files["component.css"])
    return {
        "files": files,
        "manifest": manifest,
        "tokens": tokens,
        "receipt": receipt,
        "claims": claims,
        "provenance": provenance,
        "claim_list": claim_list,
        "css_props": css_props,
        "artifact_digests": artifact_digests,
        "receipt_binding": receipt_binding,
        "checksum_binding": checksum_binding,
        "claim_binding": claim_binding,
        "artifact_binding": receipt_binding and checksum_binding and claim_binding,
        "activity_bindings": activity_binding_list,
    }


def reader_result(name: str, status: str, reason: str, matches: list[str], resolution: Any, descent: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "reader": name,
        "status": status,
        "resolution": resolution,
        "reason": reason,
        "matches": matches,
        "descent": descent or [],
        "visual_similarity": {"observed": False, "sufficient_to_close": False},
    }


def observations_for(state: dict[str, Any], intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = state["tokens"].get("tokens", [])
    observed_by_name = {item.get("name"): item for item in tokens}
    claims_by_id = {item.get("claim_id"): item for item in state["claim_list"]}
    observations = []
    for intent in intents:
        observed = observed_by_name.get(intent["name"])
        claim = claims_by_id.get(intent["claim_id"])
        observed_role = observed.get("role") if observed else None
        observed_state = claim.get("state") if claim else None
        property_exists = intent["property"] in state["css_props"]
        value_match = state["css_props"].get(intent["property"]) == intent["value"]
        observations.append({
            "source_activity": intent["source_activity"],
            "name": intent["name"],
            "property": intent["property"],
            "expected_role": intent["role"],
            "observed_role": observed_role,
            "role_match": observed_role == intent["role"],
            "expected_state": "CLOSED",
            "observed_state": observed_state,
            "state_match": observed_state == "CLOSED",
            "property_exists": property_exists,
            "value_match": value_match,
            "exact_match": bool(observed and observed_role == intent["role"] and property_exists and value_match),
        })
    return observations


def resolve_case(case_id: str, state: dict[str, Any], intents: list[dict[str, Any]], core: dict[str, Any]) -> dict[str, Any]:
    tokens = state["tokens"].get("tokens", [])
    observed_by_name = {item.get("name"): item for item in tokens}
    observations = observations_for(state, intents)
    exact_matches = [item["name"] for item in observations if item["exact_match"]]
    role_matches = [item["name"] for item in observations if any(observed.get("role") == item["expected_role"] for observed in tokens)]
    existence_matches = [item["name"] for item in observations if item["property_exists"]]
    role_contradictions = [
        {
            "kind": "ROLE_CONTRADICTION",
            "source_activity": item["source_activity"],
            "name": item["name"],
            "expected_role": item["expected_role"],
            "observed_role": item["observed_role"],
        }
        for item in observations
        if item["observed_role"] is not None and not item["role_match"]
    ]
    state_contradictions = [
        {
            "kind": "STATE_CONTRADICTION",
            "source_activity": item["source_activity"],
            "name": item["name"],
            "expected_state": item["expected_state"],
            "observed_state": item["observed_state"],
        }
        for item in observations
        if item["observed_state"] is not None and not item["state_match"]
    ]
    value_contradictions = [
        {
            "kind": "GENERATED_VALUE_CONTRADICTION",
            "source_activity": item["source_activity"],
            "name": item["name"],
            "property": item["property"],
            "expected": next(intent["value"] for intent in intents if intent["name"] == item["name"]),
            "observed": state["css_props"].get(item["property"]),
        }
        for item in observations
        if item["property_exists"] and not item["value_match"]
    ]
    contradictions = role_contradictions + state_contradictions + value_contradictions
    if state.get("stale_generated_digest"):
        contradictions.append({
            "kind": "STALE_GENERATED_DIGEST",
            "expected": "current generated artifact digests",
            "observed": "stale generated artifact digest",
        })
    if state["receipt"].get("permission", {}).get("repository_writes") != 0:
        contradictions.append({
            "kind": "AUTHORITY_ESCALATION",
            "expected_repository_writes": 0,
            "observed_repository_writes": state["receipt"]["permission"].get("repository_writes"),
        })
    bound = state["artifact_binding"] and state.get("consumer_receipt_binding", False)
    frontier = ["generated/design-tokens.json", "generated/component.css", "generated/generation-receipt.json", "generated/claim-graph.json"]
    if not bound and not contradictions:
        missing = unknown_resolution(
            step="bind-generated-evidence-chain",
            reason="GENERATED_ARTIFACT_OR_CONSUMER_RECEIPT_BINDING_MISSING",
            unknown_class="DEPENDENCY_BLOCKED",
            next_operation="RESTORE_GENERATED_ARTIFACT_AND_CONSUMER_RECEIPT_BINDING",
            blocked_by=frontier + ["consumer-receipt.json"],
        )
        descent = [
            descent_edge(source="EXACT", target="ROLE", resolution=missing),
            descent_edge(source="ROLE", target="EXISTENCE", resolution=missing),
        ]
        readers = [
            reader_result("EXACT", "UNKNOWN", "exact artifact/receipt/claim binding is unavailable", exact_matches, missing, descent),
            reader_result("ROLE", "UNKNOWN", "role resolution is dependency-blocked", role_matches, missing),
            reader_result("EXISTENCE", "UNKNOWN", "existence resolution is dependency-blocked", existence_matches, missing),
        ]
    else:
        exact_ok = len(exact_matches) == len(intents) and state["claim_binding"] and state.get("consumer_receipt_binding", False)
        role_ok = len(set(role_matches)) == len(intents) and state["claim_binding"] and state.get("consumer_receipt_binding", False)
        existence_ok = len(existence_matches) == len(intents) and state["claim_binding"] and state.get("consumer_receipt_binding", False)
        if exact_ok:
            exact = {"name": "EXACT", "basis": "token identity + value + property + generated artifact + consumer receipt + claim graph", "causal_frontier": []}
            exact_reader = reader_result("EXACT", "CLOSED", "exact semantic identity and evidence chain match", exact_matches, exact)
        else:
            missing = unknown_resolution(
                step="resolve-exact-token-identity",
                reason="EXACT_TOKEN_IDENTITY_UNAVAILABLE",
                unknown_class="DIRECT_MISSING",
                next_operation="RESTORE_EXACT_TOKEN_IDENTITY",
                blocked_by=["source token identity", "generated token identity"],
            )
            descent = [descent_edge(source="EXACT", target="ROLE", resolution=missing)]
            exact_reader = reader_result("EXACT", "UNKNOWN", "exact identity is unavailable; reader descends", exact_matches, missing, descent)
        role_resolution = {"name": "ROLE", "basis": "semantic role + observed state + generated artifact + consumer receipt + claim graph", "causal_frontier": []}
        role_unknown = unknown_resolution(
            step="resolve-semantic-role",
            reason="ROLE_OR_STATE_EVIDENCE_UNAVAILABLE",
            unknown_class="DIRECT_MISSING",
            next_operation="RESTORE_CONSUMER_OBSERVED_ROLE_AND_STATE",
            blocked_by=["consumer receipt observations", "source token roles", "generated claim graph"],
        )
        role_reader = reader_result(
            "ROLE",
            "CLOSED" if role_ok else "UNKNOWN",
            "declared semantic roles, observed state, and bound evidence match" if role_ok else "role/state evidence is incomplete",
            role_matches,
            role_resolution if role_ok else role_unknown,
            [] if role_ok else [descent_edge(source="ROLE", target="EXISTENCE", resolution=role_unknown)],
        )
        existence_resolution = {"name": "EXISTENCE", "basis": "artifact presence + consumer receipt + claim graph binding", "causal_frontier": []}
        existence_unknown = unknown_resolution(
            step="resolve-artifact-existence",
            reason="ARTIFACT_EXISTENCE_UNBOUND",
            unknown_class="DEPENDENCY_BLOCKED",
            next_operation="RESTORE_ARTIFACT_CLAIM_BINDING",
            blocked_by=["generated artifact", "consumer receipt", "claim graph"],
        )
        existence_reader = reader_result(
            "EXISTENCE",
            "CLOSED" if existence_ok else "UNKNOWN",
            "declared artifact properties exist and are evidence-bound" if existence_ok else "artifact existence is not evidence-bound",
            existence_matches,
            existence_resolution if existence_ok else existence_unknown,
        )
        readers = [exact_reader, role_reader, existence_reader]

    if contradictions:
        for reader in readers:
            reader["status"] = "REFUTED"
            reader["contradictions"] = contradictions
    status = normalize_core(core, readers)
    body = {
        "scenario_id": case_id,
        "status": status,
        "core": core,
        "readers": readers,
        "observations": observations,
        "evidence_binding": {
            "generated_artifact": state["artifact_binding"],
            "generation_receipt": state["receipt_binding"],
            "claim_graph": state["claim_binding"],
            "consumer_receipt": state.get("consumer_receipt_binding", False),
            "consumer_receipt_digest": state.get("consumer_receipt_digest"),
        },
        "refuted_over_unknown": status == "REFUTED" and any(reader["status"] == "REFUTED" for reader in readers),
        "causal_frontiers": [reader["resolution"].get("causal_frontier", []) for reader in readers if isinstance(reader.get("resolution"), dict)],
    }
    body["evidence_digest"] = digest_value(body)
    return body


def normalize_core(core: dict[str, Any], readers: list[dict[str, Any]]) -> str:
    if any(reader.get("status") == "REFUTED" for reader in readers):
        return "REFUTED"
    decision = core.get("decision")
    state = core.get("claim_state")
    occurrences = core.get("occurrences")
    reason = core.get("reason")
    if decision == "UNKNOWN" and state == "UNKNOWN" and occurrences == 0 and reason == "ACTIVITY_NOT_FOUND":
        if all(field in core.get("unknown_resolution", {}) for field in UNKNOWN_FIELDS):
            return "UNKNOWN"
        return "REFUTED"
    if decision == "REFUTED" and state == "REFUTED" and occurrences > 1 and reason == "AMBIGUOUS_ACTIVITY_BINDING":
        return "REFUTED"
    if decision == "CLOSED" and state == "CLOSED" and occurrences == 1 and reason == "EXACT_MATCH":
        if any(reader.get("status") == "UNKNOWN" for reader in readers):
            return "UNKNOWN"
        return "CLOSED"
    return "REFUTED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--denominator", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    root = args.repo_root.resolve()
    source = args.source.resolve()
    ir_path = args.ir.resolve()
    denominator = load_json(args.denominator.resolve())
    output = args.output.resolve()
    source.relative_to(root)
    args.denominator.resolve().relative_to(root)
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("consumer output must be caller-owned and outside the repository")
    bundle = args.bundle.resolve()
    source_digest, intents = source_intents(source)
    activity_records = source_activity_records(source)
    ir = load_json(ir_path)
    ir_digest = "sha256:" + str(ir["ir"]["semantic_digest"])
    expected_names = {cell["activity"] for cell in denominator["cells"]}
    ir_activity_nodes = [node for node in ir.get("nodes", []) if str(node.get("kind", "")).lower() == "activity"]
    actual_names = {node["name"] for node in ir_activity_nodes}
    if expected_names != actual_names or len(actual_names) != 12 or len(ir_activity_nodes) != 12:
        raise ValueError("consumer semantic IR activity set does not equal the product denominator")
    state = validate_bundle(bundle, source_digest, ir_digest, intents, activity_records)

    base_observations = observations_for(state, intents)
    receipt_bindings = []
    observation_by_activity = {item["source_activity"]: item for item in base_observations}
    token_activities = {item["source_activity"] for item in intents}
    for binding in state["activity_bindings"]:
        activity = binding["source_activity"]["name"]
        observed = observation_by_activity.get(activity)
        receipt_bindings.append({
            **binding,
            "consumer_receipt": "consumer-receipt.json",
            "consumer_observed": observed if activity in token_activities else {
                "source_activity": activity,
                "observed_role": None,
                "observed_state": "CLOSED" if state["artifact_binding"] else "UNKNOWN",
                "role_match": True,
                "state_match": state["artifact_binding"],
            },
        })

    normal_core = {"decision": "CLOSED", "claim_state": "CLOSED", "occurrences": 1, "reason": "EXACT_MATCH"}
    preview_state = copy.deepcopy(state)
    preview_state["consumer_receipt_binding"] = True
    preview_state["consumer_receipt_digest"] = "sha256:pending-consumer-receipt"
    normal_preview = resolve_case("normal", preview_state, intents, normal_core)
    receipt_body = {
        "schema": "gooo/design-contract-bridge/consumer-receipt/v2",
        "source_digest": source_digest,
        "ir_digest": ir_digest,
        "bundle": {"path": str(bundle), "files": len(BUNDLE_FILES), "artifact_binding": state["artifact_binding"]},
        "readers": ["EXACT", "ROLE", "EXISTENCE"],
        "normal": {"status": normal_preview["status"], "reader_statuses": {reader["reader"]: reader["status"] for reader in normal_preview["readers"]}},
        "activity_bindings": receipt_bindings,
        "activity_binding_digest": digest_value(receipt_bindings),
        "observations": base_observations,
        "semantic_match_requires": ["generated artifact", "consumer receipt", "claim graph"],
        "visual_similarity_can_close": False,
    }
    receipt_body_digest = digest_value(receipt_body)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "consumer-receipt.json", {**receipt_body, "receipt_digest": receipt_body_digest})
    state["consumer_receipt_binding"] = True
    state["consumer_receipt_digest"] = digest_bytes((output / "consumer-receipt.json").read_bytes())
    state["consumer_receipt_body_digest"] = receipt_body_digest
    normal = resolve_case("normal", copy.deepcopy(state), intents, normal_core)

    exact_unknown_state = copy.deepcopy(state)
    first_token = next(token for token in exact_unknown_state["tokens"]["tokens"] if token["name"] == intents[0]["name"])
    first_token["name"] = "color.surface.canvas.alias"
    unknown_core = {
        "decision": "UNKNOWN", "claim_state": "UNKNOWN", "occurrences": 0, "reason": "ACTIVITY_NOT_FOUND",
        "unknown_resolution": unknown_resolution(
            step="resolve-exact-token-identity", reason="EXACT_TOKEN_IDENTITY_UNAVAILABLE", unknown_class="DIRECT_MISSING",
            next_operation="RESTORE_EXACT_TOKEN_IDENTITY", blocked_by=["source token identity", "generated token identity"],
        ),
    }
    unknown = resolve_case("unknown-exact-identity", exact_unknown_state, intents, unknown_core)

    missing_receipt_state = copy.deepcopy(state)
    missing_receipt_state["consumer_receipt_binding"] = False
    missing_receipt_state["consumer_receipt_digest"] = None
    missing_consumer_receipt = resolve_case("missing-consumer-receipt", missing_receipt_state, intents, {
        "decision": "UNKNOWN", "claim_state": "UNKNOWN", "occurrences": 0, "reason": "ACTIVITY_NOT_FOUND",
        "unknown_resolution": unknown_resolution(
            step="bind-consumer-receipt",
            reason="CONSUMER_RECEIPT_MISSING",
            unknown_class="DEPENDENCY_BLOCKED",
            next_operation="RESTORE_CONSUMER_RECEIPT",
            blocked_by=["consumer-receipt.json", "consumer receipt digest"],
        ),
    })

    dependency_unknown_state = copy.deepcopy(state)
    dependency_unknown_state["artifact_binding"] = False
    dependency_unknown_state["claim_binding"] = False
    dependency_unknown = resolve_case("unknown-dependency-blocked", dependency_unknown_state, intents, {
        "decision": "UNKNOWN", "claim_state": "UNKNOWN", "occurrences": 0, "reason": "ACTIVITY_NOT_FOUND",
        "unknown_resolution": unknown_resolution(
            step="bind-generated-evidence-chain", reason="GENERATED_ARTIFACT_BINDING_MISSING", unknown_class="DEPENDENCY_BLOCKED",
            next_operation="RESTORE_GENERATED_ARTIFACT_BINDING", blocked_by=["generated/generation-receipt.json", "generated/claim-graph.json"],
        ),
    })

    role_contradiction_state = copy.deepcopy(state)
    role_token = next(token for token in role_contradiction_state["tokens"]["tokens"] if token["name"] == intents[0]["name"])
    role_token["role"] = "text"
    role_claim = next(claim for claim in role_contradiction_state["claim_list"] if claim["claim_id"] == intents[0]["claim_id"])
    role_claim["state"] = "UNKNOWN"
    role_contradiction = resolve_case("role-contradiction", role_contradiction_state, intents, {
        "decision": "REFUTED", "claim_state": "REFUTED", "occurrences": 2, "reason": "AMBIGUOUS_ACTIVITY_BINDING",
    })

    refuted_state = copy.deepcopy(state)
    refuted_state["css_props"][intents[0]["property"]] = "#000000"
    refuted = resolve_case("refuted-value-and-state", refuted_state, intents, {
        "decision": "REFUTED", "claim_state": "REFUTED", "occurrences": 2, "reason": "AMBIGUOUS_ACTIVITY_BINDING",
    })

    stale_state = copy.deepcopy(state)
    stale_state["stale_generated_digest"] = True
    stale_state["artifact_binding"] = False
    stale_state["checksum_binding"] = False
    stale_state["receipt"]["generated_artifact_digests"]["component.css"] = "sha256:stale-generated-digest"
    stale = resolve_case("stale-generated-digest", stale_state, intents, {
        "decision": "REFUTED", "claim_state": "REFUTED", "occurrences": 2, "reason": "AMBIGUOUS_ACTIVITY_BINDING",
    })

    malformed = copy.deepcopy(unknown)
    malformed["scenario_id"] = "malformed-unknown-tuple"
    malformed["core"]["unknown_resolution"].pop("blocked_by", None)
    malformed["status"] = normalize_core(malformed["core"], malformed["readers"])
    malformed["failure_code"] = "UNKNOWN_TUPLE_INCOMPLETE"
    malformed["evidence_digest"] = digest_value({key: value for key, value in malformed.items() if key != "evidence_digest"})
    fixed_point = {
        "scenario_id": "fixed-point-core-decision",
        "status": normalize_core({"decision": "FIXED_POINT", "claim_state": "FIXED_POINT", "occurrences": 1, "reason": "UNRECOGNIZED_STATE"}, []),
        "core": {"decision": "FIXED_POINT", "claim_state": "FIXED_POINT", "occurrences": 1, "reason": "UNRECOGNIZED_STATE"},
        "failure_code": "UNRECOGNIZED_CORE_DECISION",
    }
    fixed_point["evidence_digest"] = digest_value(fixed_point)
    authority_state = copy.deepcopy(state)
    authority_state["receipt"]["permission"]["repository_writes"] = 1
    authority = resolve_case("authority-escalation", authority_state, intents, {
        "decision": "REFUTED", "claim_state": "REFUTED", "occurrences": 2, "reason": "AMBIGUOUS_ACTIVITY_BINDING",
    })
    authority["failure_code"] = "AUTHORITY_ESCALATED"
    authority["evidence_digest"] = digest_value({key: value for key, value in authority.items() if key != "evidence_digest"})

    scenarios = [normal, unknown, missing_consumer_receipt, dependency_unknown, role_contradiction, refuted, stale, malformed, fixed_point, authority]
    counts = {status: sum(item["status"] == status for item in scenarios) for status in ("CLOSED", "UNKNOWN", "REFUTED")}
    write_json(output / "match-report.json", {
        "schema": "gooo/design-contract-bridge/semantic-match-report/v2",
        "status": normal["status"],
        "readers": normal["readers"],
        "observations": normal["observations"],
        "source_intents": len(intents),
        "generated_tokens": len(state["tokens"].get("tokens", [])),
        "claim_graph_claims": len(state["claim_list"]),
        "artifact_receipt_claim_binding": state["artifact_binding"],
        "evidence_binding": normal["evidence_binding"],
        "consumer_receipt_digest": state["consumer_receipt_digest"],
        "consumer_receipt_body_digest": state["consumer_receipt_body_digest"],
        "activity_binding_digest": digest_value(state["activity_bindings"]),
    })
    write_json(output / "challenge-report.json", {
        "schema": "gooo/design-contract-bridge/challenge-report/v2",
        "precedence": ["REFUTED", "UNKNOWN", "CLOSED"],
        "unknown_fields": UNKNOWN_FIELDS,
        "scenarios": scenarios,
        "counts": {"denominator": len(scenarios), **counts},
        "minimum_cases": {"CLOSED": 1, "UNKNOWN": 1, "REFUTED": 1},
    })
    write_json(output / "normalized-core.json", {
        "schema": "gooo/design-contract-bridge/normalized-core/v2",
        "accepted_tuples": {
            "CLOSED": {"decision": "CLOSED", "claim_state": "CLOSED", "occurrences": 1, "reason": "EXACT_MATCH"},
            "UNKNOWN": {"decision": "UNKNOWN", "claim_state": "UNKNOWN", "occurrences": 0, "reason": "ACTIVITY_NOT_FOUND", "unknown_fields": UNKNOWN_FIELDS},
            "REFUTED": {"decision": "REFUTED", "claim_state": "REFUTED", "occurrences": ">1", "reason": "AMBIGUOUS_ACTIVITY_BINDING"},
        },
        "scenario_statuses": {item["scenario_id"]: item["status"] for item in scenarios},
        "status_precedence": ["REFUTED", "UNKNOWN", "CLOSED"],
        "fixed_point": "REFUTED",
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"design consumer: {exc}")
        raise SystemExit(1)
