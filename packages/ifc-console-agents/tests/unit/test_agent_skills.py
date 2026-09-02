"""The structured and prose skill store and its public tools."""

from __future__ import annotations

import pytest
from ifc_console.core.results import ToolError

from ifc_console_agents.skills import (
    AgentSkillStore,
    MeasurementApplicability,
    MeasurementExemplar,
    MeasurementExemplarObject,
    MeasurementIntent,
    MeasurementModelRevision,
    MeasurementRule,
    MeasurementSkillSpec,
    MeasurementTolerance,
    MeasurementVerification,
    measurement_spec_block,
    parse_measurement_spec,
)

BODY = "## When to use\nSheet piles.\n\n## Steps\n1. analyze_element_geometry\n"
GUID = "2O2Fr$t4X7Zf8NOew3FL9r"
SIGNATURE = {
    "version": "1.0",
    "class_family": "linear_member",
    "ifc_class": "IfcMember",
    "type_key": "ifcmembertype:ipe200",
    "geometry_family": "constant_profile_extrusion",
    "profile_family": "i_shape",
    "material_key": "steel",
    "components": 1,
    "closed_shells": 1,
    "through_holes": 0,
    "section_variation": "constant",
    "normalized_extents": [1.0, 0.1, 0.05],
    "normalized_section_bounds": [1.0, 0.5],
    "fingerprint": "sha256:example",
}


def _spec(*, unresolved: bool = False, signature: dict | None = SIGNATURE):
    return MeasurementSkillSpec(
        applicability=MeasurementApplicability(
            ifc_classes=("IfcMember",),
            profile_families=("i_shape",) if signature and signature.get("profile_family") else (),
            geometry_families=("constant_profile_extrusion",)
            if signature and signature.get("geometry_family")
            else (),
            similarity_threshold=0.85,
        ),
        measurements=(
            MeasurementRule(
                output=None if unresolved else "profile.web_thickness",
                unresolved=unresolved,
                preferred_sources=("profile_parameter",),
                intent=MeasurementIntent(
                    viewer_kind="distance",
                    viewer_index=0,
                    value_si=0.01,
                    semantic_direction="transverse",
                    local_direction=(0.0, 1.0, 0.0),
                ),
            ),
        ),
        exemplar=MeasurementExemplar(
            model_revision=MeasurementModelRevision(
                model_id="model-a", fingerprint="fp-a", revision=2
            ),
            objects=(
                MeasurementExemplarObject(
                    global_id=GUID,
                    ifc_class="IfcMember",
                    type_name="IPE200",
                    geometry_family="constant_profile_extrusion",
                    geometry_signature=signature or {},
                ),
            ),
        ),
        outputs=() if unresolved else ("profile.web_thickness",),
    )


def _structured_body(*, unresolved: bool = False, signature: dict | None = SIGNATURE) -> str:
    return BODY + "\n" + measurement_spec_block(_spec(unresolved=unresolved, signature=signature))


def _body_for(spec: MeasurementSkillSpec) -> str:
    return BODY + "\n" + measurement_spec_block(spec)


def _rule_spec(
    rule: MeasurementRule,
    *,
    verification: MeasurementVerification | None = None,
) -> MeasurementSkillSpec:
    base = _spec()
    return MeasurementSkillSpec(
        applicability=base.applicability,
        measurements=(rule,),
        exemplar=base.exemplar,
        verification=verification or MeasurementVerification(),
        outputs=(rule.output,) if rule.output else (),
    )


class TestSkillStore:
    def test_save_read_and_list_round_trip(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        row = store.save(
            "sheet-pile-profile",
            BODY,
            description="Measure a sheet pile profile",
            applies_to="IfcMember sheet piles",
        )
        assert row["name"] == "sheet-pile-profile"
        assert row["path"].endswith("sheet-pile-profile.md")

        loaded = store.read("sheet-pile-profile")
        assert loaded["description"] == "Measure a sheet pile profile"
        assert loaded["applies_to"] == "IfcMember sheet piles"
        assert "analyze_element_geometry" in loaded["content"]
        assert "---" not in loaded["content"]

        entries = store.entries()
        assert [entry["name"] for entry in entries] == ["sheet-pile-profile"]
        assert "content" not in entries[0]

    def test_overwrite_is_explicit(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("a-skill", BODY, description="one")
        with pytest.raises(ToolError) as caught:
            store.save("a-skill", BODY, description="two")
        assert caught.value.code == "FILE_EXISTS"
        store.save("a-skill", BODY, description="two", overwrite=True)
        assert store.read("a-skill")["description"] == "two"

    def test_names_are_slugs_only(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        for bad in ("../escape", "UPPER", "a b", "", "x"):
            with pytest.raises(ToolError):
                store.save(bad, BODY, description="nope")

    def test_a_missing_skill_names_what_exists(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("known", BODY, description="here")
        with pytest.raises(ToolError) as caught:
            store.read("unknown")
        assert caught.value.code == "NOT_FOUND"
        assert "known" in caught.value.hint

    def test_import_takes_external_markdown_as_it_comes(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        with_header = (
            b"---\nname: pile-check\ndescription: Check a pile\napplies_to: IfcPile\n---\n\n"
            b"Steps here.\n"
        )
        row = store.import_file("Anything At All.md", with_header)
        assert row["name"] == "pile-check"
        assert store.read("pile-check")["applies_to"] == "IfcPile"

        bare = b"# Measure openings\n\n1. query_elements\n"
        row = store.import_file("Measure Openings (v2).md", bare)
        assert row["name"] == "measure-openings-v2"
        assert row["description"] == "Measure openings"

        again = store.import_file("Anything.md", with_header)
        assert again["name"] == "pile-check-2"

        with pytest.raises(ToolError) as caught:
            store.import_file("x" * 40 + ".md", b"a" * (64 * 1024 + 1))
        assert caught.value.code == "INVALID_INPUT"

    def test_hand_written_front_matter_is_parsed(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.directory.mkdir(parents=True)
        (store.directory / "manual.md").write_text(
            "---\nname: manual\ndescription: written by hand\n---\n\nBody text.\n",
            encoding="utf-8",
        )
        entry = store.entries()[0]
        assert entry["name"] == "manual"
        assert entry["description"] == "written by hand"

    def test_structured_skill_infers_front_matter_and_round_trips_one_spec(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        row = store.save("web-thickness", _structured_body(), description="Measure web")
        assert row["kind"] == "parametric_measurement"
        assert row["schema_version"] == 2
        assert row["executable"] is True

        raw = store.path_for("web-thickness").read_text(encoding="utf-8")
        assert "kind: parametric_measurement" in raw
        assert "schema_version: 2" in raw
        assert raw.count("```measurement-spec") == 1
        parsed = parse_measurement_spec(raw, required=True)
        assert parsed is not None
        assert parsed.measurement_ids == ("profile.web_thickness",)

    def test_hand_authored_v2_rule_does_not_require_recording_metadata(self):
        parsed = MeasurementSkillSpec.model_validate(
            {
                "schema_version": 2,
                "kind": "parametric_measurement",
                "applicability": {"ifc_classes": ["IfcMember"]},
                "measurements": [
                    {
                        "output": "profile.web_thickness",
                        "preferred_sources": ["profile_parameter", "mesh_section"],
                        "frame": "semantic",
                        "minimum_confidence": "medium",
                        "tolerance": {"absolute_si": 0.001, "relative": 0.02},
                    }
                ],
                "outputs": [],
            }
        )
        assert parsed.measurement_ids == ("profile.web_thickness",)
        assert parsed.measurements[0].intent.viewer_kind == "unknown"

    @pytest.mark.parametrize(
        "content",
        [
            "```measurement-spec\n{not json}\n```",
            "```measurement-spec\n{}\n```",
            _structured_body() + "\n" + measurement_spec_block(_spec()),
            "```measurement-spec\n{}",
        ],
    )
    def test_malformed_or_multiple_measurement_specs_are_refused(self, tmp_path, content):
        store = AgentSkillStore(tmp_path)
        with pytest.raises(ToolError) as caught:
            store.save("bad-spec", content, description="bad")
        assert caught.value.code == "INVALID_INPUT"

    def test_prose_skills_keep_loading_with_explicit_legacy_status(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("old-method", BODY, description="old")
        loaded = store.read("old-method")
        assert loaded["kind"] == "prose"
        assert loaded["spec_status"] == "none"
        assert loaded["executable"] is False
        with pytest.raises(ToolError) as caught:
            store.measurement_spec("old-method")
        assert caught.value.code == "INVALID_INPUT"
        assert "prose-only" in caught.value.message

    def test_prose_migration_preview_is_valid_reviewable_and_non_mutating(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        prose = (
            "## When to use\nIfcWall elements.\n\n"
            "## Steps\nMeasure profile.web_thickness and overall height.\n"
        )
        store.save(
            "old-wall-method",
            prose,
            description="Old wall method",
            applies_to="IfcWall",
        )
        path = store.path_for("old-wall-method")
        before = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        preview = store.migration_preview("old-wall-method")

        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == before_mtime
        assert store.read("old-wall-method")["spec_status"] == "none"
        assert preview["read_only"] is True
        assert preview["source_unchanged"] is True
        assert preview["side_effects"]["file_writes"] == 0
        suggestion = preview["suggestion"]
        assert suggestion["review_required"] is True
        assert suggestion["executable"] is False
        assert suggestion["inferred"]["ifc_classes"] == ["IfcWall"]
        assert suggestion["inferred"]["candidate_outputs"] == [
            "profile.web_thickness",
            "envelope.overall_height",
        ]
        spec = MeasurementSkillSpec.model_validate(suggestion["measurement_spec"])
        assert spec.executable is False
        assert all(rule.unresolved for rule in spec.measurements)
        parsed = parse_measurement_spec(suggestion["content"], required=True)
        assert parsed == spec

    def test_structured_skill_is_not_given_a_second_migration_spec(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("already-v2", _structured_body(), description="v2")
        with pytest.raises(ToolError) as caught:
            store.migration_preview("already-v2")
        assert caught.value.code == "INVALID_INPUT"
        assert "already contains" in caught.value.message


class TestSkillTools:
    async def _ops(self, core):
        from ifc_console.application.operations import build_operations

        return build_operations(core)

    async def test_the_agent_can_record_and_reuse_a_skill(self, core):
        ops = await self._ops(core)
        saved = await ops.call(
            "save_agent_skill",
            {
                "name": "sheet-pile-profile",
                "description": "Measure a sheet pile",
                "content": BODY,
                "applies_to": "IfcMember",
            },
        )
        assert saved.ok is True
        assert saved.data["saved"] is True

        listed = await ops.call("list_agent_skills", {})
        assert listed.ok is True
        assert listed.data["count"] == 1
        assert listed.data["skills"][0]["name"] == "sheet-pile-profile"

        loaded = await ops.call("get_agent_skill", {"name": "sheet-pile-profile"})
        assert loaded.ok is True
        assert "analyze_element_geometry" in loaded.data["content"]

    async def test_an_empty_project_hints_at_recording(self, core):
        ops = await self._ops(core)
        listed = await ops.call("list_agent_skills", {})
        assert listed.data["count"] == 0
        assert "save_agent_skill" in listed.data["note"]

    async def test_a_tool_argument_named_name_survives_the_agent_toolset(self, core):
        """Regression: `name` used to collide with the workbench call's own
        first parameter and die as TOOL_SOURCE_FAILED before reaching the tool."""
        from ifc_console_agents.panel import panel_runtime

        await self._ops(core)
        toolset = await panel_runtime(core).toolset()
        result = await toolset.call("get_agent_skill", {"name": "does-not-exist"})
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

        saved = await toolset.call(
            "save_agent_skill",
            {"name": "via-panel", "description": "d", "content": BODY},
        )
        assert saved["ok"] is True

    async def test_saving_requires_approval_in_agent_surfaces(self, core):
        from ifc_console_agents.panel import panel_runtime

        await self._ops(core)
        toolset = await panel_runtime(core).toolset()
        by_name = {definition.name: definition for definition in toolset.definitions}
        assert by_name["save_agent_skill"].requires_approval is True
        assert by_name["get_agent_skill"].requires_approval is False
        assert by_name["preview_measurement_skill_migration"].requires_approval is False
        assert by_name["apply_measurement_skill"].requires_approval is False

    async def test_migration_preview_tool_never_writes_the_prose_skill(self, core):
        ops = await self._ops(core)
        store = AgentSkillStore(core.store.project_dir)
        store.save(
            "legacy-profile",
            "## Steps\nFor IfcMember, measure web thickness.\n",
            description="legacy",
            applies_to="IfcMember",
        )
        path = store.path_for("legacy-profile")
        before = path.read_bytes()

        result = await ops.call("preview_measurement_skill_migration", {"name": "legacy-profile"})

        assert result.ok is True
        assert result.data["read_only"] is True
        assert result.data["source_unchanged"] is True
        assert result.data["side_effects"]["file_writes"] == 0
        assert result.data["suggestion"]["inferred"]["candidate_outputs"] == [
            "profile.web_thickness"
        ]
        assert path.read_bytes() == before
        assert store.read("legacy-profile")["kind"] == "prose"

    async def test_legacy_and_malformed_skills_refuse_deterministic_apply(self, core):
        ops = await self._ops(core)
        store = AgentSkillStore(core.store.project_dir)
        store.save("old-method", BODY, description="old")
        legacy = await ops.call(
            "apply_measurement_skill", {"name": "old-method", "global_ids": [GUID]}
        )
        assert legacy.ok is False
        assert legacy.error.code == "INVALID_INPUT"
        assert "prose-only" in legacy.error.message

        store.directory.mkdir(parents=True, exist_ok=True)
        store.path_for("broken-spec").write_text(
            "---\nname: broken-spec\ndescription: bad\nkind: parametric_measurement\n"
            "schema_version: 2\n---\n\n```measurement-spec\n{bad}\n```\n",
            encoding="utf-8",
        )
        malformed = await ops.call(
            "apply_measurement_skill", {"name": "broken-spec", "global_ids": [GUID]}
        )
        assert malformed.ok is False
        assert malformed.error.code == "INVALID_INPUT"
        assert "invalid measurement spec" in malformed.error.message

    async def test_unresolved_recorded_intent_requires_review_before_replay(self, core):
        ops = await self._ops(core)
        AgentSkillStore(core.store.project_dir).save(
            "needs-review", _structured_body(unresolved=True), description="review"
        )
        result = await ops.call(
            "apply_measurement_skill", {"name": "needs-review", "global_ids": [GUID]}
        )
        assert result.ok is False
        assert result.error.code == "VALIDATION_FAILED"
        assert "unresolved" in result.error.message

    async def test_apply_is_dry_run_read_only_and_extracts_the_exemplar(self, core, monkeypatch):
        ops = await self._ops(core)
        AgentSkillStore(core.store.project_dir).save(
            "web-thickness", _structured_body(), description="web"
        )
        calls = []

        async def fake_call(_self, operation, **arguments):
            calls.append((operation, arguments))
            assert operation == "analyze_element_geometry"
            return {
                "ok": True,
                "data": {
                    "analysis_version": "2.0",
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-a",
                        "revision": 2,
                    },
                    "elements": [
                        {
                            "object": {
                                "global_id": GUID,
                                "class": "IfcMember",
                                "name": "M-01",
                                "type": "IPE200",
                            },
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": SIGNATURE,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.012,
                                    "si_unit": "m",
                                    "source": "profile_parameter",
                                    "method": "ifc_representation",
                                    "frame": "semantic",
                                    "direction": "transverse",
                                    "confidence": "high",
                                    "flags": [],
                                }
                            ],
                            "coverage": {
                                "extracted": ["profile.web_thickness"],
                                "unavailable": [],
                                "ambiguous": [],
                                "conflicting": [],
                            },
                            "flags": [],
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {"name": "web-thickness", "global_ids": [GUID]},
        )
        assert result.ok is True
        assert result.data["dry_run"] is True
        assert result.data["read_only"] is True
        assert result.data["side_effects"] == {
            "property_writes": 0,
            "proposals": 0,
            "file_writes": 0,
        }
        assert result.data["results"][0]["status"] == "extracted"
        assert result.data["results"][0]["extracted"][0]["value_si"] == 0.012
        applicability = result.data["results"][0]["applicability"]
        assert applicability["exemplar_identity_match"] is True
        assert applicability["pinned_revision_match"] is True
        assert applicability["geometry_compared"] is False
        assert applicability["comparison_basis"] == "pinned_exemplar_revision"
        assert applicability["comparison_exemplar_global_id"] == GUID
        assert result.data["skill"]["exemplar"]["object_count"] == 1
        assert result.data["skill"]["exemplar"]["objects"][0]["global_id"] == GUID
        assert result.data["targets"]["all_page_targets_reported"] is True
        assert result.data["summary"]["exemplar_targets_returned"] == 1
        assert result.data["summary"]["exemplar_targets_applicable"] == 1
        assert [name for name, _ in calls] == ["analyze_element_geometry"]
        analysis_arguments = calls[0][1]
        assert analysis_arguments["detail"] == "compact"
        assert analysis_arguments["measurement_ids"] == ["profile.web_thickness"]

    async def test_same_class_without_geometry_evidence_is_skipped(self, core, monkeypatch):
        ops = await self._ops(core)
        class_only = {"version": "1.0", "class_family": "linear_member"}
        AgentSkillStore(core.store.project_dir).save(
            "class-only",
            _structured_body(signature=class_only),
            description="unsafe broad match",
        )

        async def fake_call(_self, operation, **_arguments):
            assert operation == "analyze_element_geometry"
            return {
                "ok": True,
                "data": {
                    "model_revision": {},
                    "elements": [
                        {
                            "object": {
                                "global_id": "0JZG1wYVj0Hf8gPr0other",
                                "class": "IfcMember",
                            },
                            "geometry_signature": class_only,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.01,
                                    "confidence": "high",
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {
                "name": "class-only",
                "global_ids": ["0JZG1wYVj0Hf8gPr0other"],
            },
        )
        assert result.ok is True
        row = result.data["results"][0]
        assert row["status"] == "skipped"
        assert row["applicability"]["applicable"] is False
        assert any(
            "same-class membership alone" in value for value in row["applicability"]["mismatches"]
        )

    async def test_selector_application_is_paged_and_capped(self, core, monkeypatch):
        ops = await self._ops(core)
        AgentSkillStore(core.store.project_dir).save(
            "paged-web", _structured_body(), description="paged"
        )
        calls = []

        async def fake_call(_self, operation, **arguments):
            calls.append((operation, arguments))
            if operation == "query_elements":
                return {
                    "ok": True,
                    "data": {"rows": [{"global_id": GUID}]},
                    "meta": {"total": 12},
                }
            assert operation == "analyze_element_geometry"
            return {"ok": True, "data": {"model_revision": {}, "elements": []}, "meta": {}}

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {
                "name": "paged-web",
                "selector": "IfcMember",
                "offset": 2,
                "limit": 3,
                "max_matches": 5,
            },
        )
        assert result.ok is True
        assert result.data["targets"]["matched"] == 12
        assert result.data["targets"]["capped_total"] == 5
        assert result.data["targets"]["truncated_by_max_matches"] is True
        assert calls[0][0] == "query_elements"
        assert calls[0][1]["limit"] == 3
        assert calls[0][1]["offset"] == 2

    async def test_nested_analysis_is_per_target_and_truncation_becomes_a_failed_row(
        self, core, monkeypatch
    ):
        ops = await self._ops(core)
        AgentSkillStore(core.store.project_dir).save(
            "bounded-web", _structured_body(), description="bounded"
        )
        target_ids = ["target-a", "target-b", "target-c"]
        calls = []

        async def fake_call(_self, operation, **arguments):
            assert operation == "analyze_element_geometry"
            calls.append(arguments)
            target_id = arguments["global_ids"][0]
            if target_id == "target-b":
                return {
                    "ok": True,
                    "data": {
                        "truncation": {"key": "elements", "kept": 0, "of": 1},
                        "elements": [],
                    },
                    "meta": {"truncated": True},
                }
            return {
                "ok": True,
                "data": {
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-a",
                        "revision": 2,
                    },
                    "elements": [
                        {
                            "object": {"global_id": target_id, "class": "IfcMember"},
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": SIGNATURE,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.01,
                                    "source": "profile_parameter",
                                    "confidence": "high",
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {"name": "bounded-web", "global_ids": target_ids, "limit": 3},
        )
        assert result.ok is True
        assert [row["object"]["global_id"] for row in result.data["results"]] == target_ids
        assert [row["status"] for row in result.data["results"]] == [
            "extracted",
            "failed",
            "extracted",
        ]
        failed = result.data["results"][1]
        assert failed["error"]["code"] == "RESULT_TOO_LARGE"
        assert failed["skipped"][0]["output"] == "profile.web_thickness"
        assert len(calls) == 3
        assert all(call["max_elements"] == 1 for call in calls)
        assert all(len(call["global_ids"]) == 1 for call in calls)

    async def test_same_exemplar_guid_with_changed_revision_must_still_match_geometry(
        self, core, monkeypatch
    ):
        ops = await self._ops(core)
        AgentSkillStore(core.store.project_dir).save(
            "changed-exemplar", _structured_body(), description="changed"
        )
        changed_signature = {
            **SIGNATURE,
            "type_key": "ifcmembertype:other",
            "material_key": "concrete",
            "components": 9,
            "closed_shells": 0,
            "through_holes": 8,
            "section_variation": "tapered",
            "normalized_extents": [1.0, 1.0, 1.0],
            "normalized_section_bounds": [0.0, 1.0],
            "fingerprint": "sha256:changed",
        }

        async def fake_call(_self, operation, **_arguments):
            assert operation == "analyze_element_geometry"
            return {
                "ok": True,
                "data": {
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-changed",
                        "revision": 3,
                    },
                    "elements": [
                        {
                            "object": {"global_id": GUID, "class": "IfcMember"},
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": changed_signature,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.01,
                                    "source": "profile_parameter",
                                    "confidence": "high",
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {"name": "changed-exemplar", "global_ids": [GUID]},
        )
        assert result.ok is True
        row = result.data["results"][0]
        assert row["status"] == "skipped"
        assert row["applicability"]["applicable"] is False
        assert row["applicability"]["score"] < row["applicability"]["threshold"]
        assert row["applicability"]["exemplar_identity_match"] is True
        assert row["applicability"]["pinned_revision_match"] is False
        assert row["applicability"]["geometry_compared"] is True
        assert row["applicability"]["comparison_basis"] == "geometry_signature"
        assert row["applicability"]["comparison_exemplar_global_id"] == GUID
        assert any("geometry was compared" in reason for reason in row["applicability"]["reasons"])

    async def test_rule_semantics_select_a_compatible_preferred_alternative(
        self, core, monkeypatch
    ):
        ops = await self._ops(core)
        rule = MeasurementRule(
            output="profile.web_thickness",
            rule_type="object_measurement",
            preferred_sources=("profile_parameter", "mesh_section"),
            fallbacks=("adaptive_section.thickness_modes",),
            frame="semantic",
            direction="transverse",
            tolerance=MeasurementTolerance(absolute_si=0.001, relative=0.0),
            intent=MeasurementIntent(viewer_kind="distance", viewer_index=0),
        )
        spec = _rule_spec(rule, verification=MeasurementVerification(cross_check="none"))
        AgentSkillStore(core.store.project_dir).save(
            "semantic-source", _body_for(spec), description="semantic source"
        )
        calls = []

        async def fake_call(_self, operation, **arguments):
            assert operation == "analyze_element_geometry"
            calls.append(arguments)
            return {
                "ok": True,
                "data": {
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-a",
                        "revision": 2,
                    },
                    "elements": [
                        {
                            "object": {"global_id": GUID, "class": "IfcMember"},
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": SIGNATURE,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.0101,
                                    "source": "mesh_section",
                                    "method": "adaptive_section",
                                    "frame": "semantic",
                                    "direction": "transverse",
                                    "confidence": "high",
                                    "alternatives": [
                                        {
                                            "value_si": 0.03,
                                            "quantity_kind": "length",
                                            "source": "profile_parameter",
                                            "method": "ifc_representation",
                                            "frame": "world",
                                            "direction": "vertical",
                                            "confidence": "exact",
                                        },
                                        {
                                            "value_si": 0.04,
                                            "quantity_kind": "area",
                                            "source": "profile_parameter",
                                            "method": "ifc_representation",
                                            "frame": "semantic",
                                            "direction": "transverse",
                                            "confidence": "exact",
                                        },
                                        {
                                            "value_si": 0.01,
                                            "quantity_kind": "length",
                                            "source": "profile_parameter",
                                            "method": "ifc_representation",
                                            "frame": "semantic",
                                            "direction": "transverse",
                                            "confidence": "high",
                                        },
                                    ],
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        result = await ops.call(
            "apply_measurement_skill",
            {"name": "semantic-source", "global_ids": [GUID]},
        )
        assert result.ok is True
        extracted = result.data["results"][0]["extracted"][0]
        assert extracted["value_si"] == 0.01
        assert extracted["source"] == "profile_parameter"
        assert extracted["selected_from"] == "alternative"
        assert extracted["verification"]["status"] == "not_requested"
        assert calls[0]["include_alternatives"] is True
        assert calls[0]["frame"] == "semantic"

    async def test_explicit_supported_fallback_and_unsupported_rule_type(self, core, monkeypatch):
        ops = await self._ops(core)
        fallback_rule = MeasurementRule(
            output="profile.web_thickness",
            preferred_sources=("profile_parameter",),
            fallbacks=("adaptive_section.thickness_modes",),
            frame="semantic",
            direction="transverse",
            intent=MeasurementIntent(viewer_kind="distance", viewer_index=0),
        )
        relationship_rule = MeasurementRule(
            output="profile.web_thickness",
            rule_type="relationship",
            preferred_sources=("profile_parameter",),
            frame="semantic",
            direction="transverse",
            intent=MeasurementIntent(viewer_kind="distance", viewer_index=0),
        )
        store = AgentSkillStore(core.store.project_dir)
        store.save(
            "supported-fallback",
            _body_for(
                _rule_spec(
                    fallback_rule,
                    verification=MeasurementVerification(cross_check="none"),
                )
            ),
            description="fallback",
        )
        store.save(
            "unsupported-relation",
            _body_for(
                _rule_spec(
                    relationship_rule,
                    verification=MeasurementVerification(cross_check="none"),
                )
            ),
            description="relationship",
        )

        async def fake_call(_self, operation, **_arguments):
            assert operation == "analyze_element_geometry"
            return {
                "ok": True,
                "data": {
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-a",
                        "revision": 2,
                    },
                    "elements": [
                        {
                            "object": {"global_id": GUID, "class": "IfcMember"},
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": SIGNATURE,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.011,
                                    "source": "mesh_section",
                                    "method": "adaptive_section",
                                    "frame": "semantic",
                                    "direction": "transverse",
                                    "confidence": "high",
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        fallback = await ops.call(
            "apply_measurement_skill",
            {"name": "supported-fallback", "global_ids": [GUID]},
        )
        extracted = fallback.data["results"][0]["extracted"][0]
        assert extracted["fallback_used"] == "adaptive_section.thickness_modes"
        unsupported = await ops.call(
            "apply_measurement_skill",
            {"name": "unsupported-relation", "global_ids": [GUID]},
        )
        skipped = unsupported.data["results"][0]["skipped"][0]
        assert "rule_type relationship is not supported" in skipped["reason"]

    async def test_second_source_conflict_honors_on_conflict_policy(self, core, monkeypatch):
        ops = await self._ops(core)
        rule = MeasurementRule(
            output="profile.web_thickness",
            preferred_sources=("profile_parameter",),
            fallbacks=("adaptive_section.thickness_modes",),
            frame="semantic",
            direction="transverse",
            tolerance=MeasurementTolerance(absolute_si=0.0001, relative=0.0),
            intent=MeasurementIntent(viewer_kind="distance", viewer_index=0),
        )
        store = AgentSkillStore(core.store.project_dir)
        store.save("refuse-conflict", _body_for(_rule_spec(rule)), description="refuse")
        report_spec = _rule_spec(
            rule,
            verification=MeasurementVerification(on_conflict="report"),
        )
        store.save("report-conflict", _body_for(report_spec), description="report")

        async def fake_call(_self, operation, **arguments):
            assert operation == "analyze_element_geometry"
            assert arguments["include_alternatives"] is True
            return {
                "ok": True,
                "data": {
                    "model_revision": {
                        "model_id": "model-a",
                        "fingerprint": "fp-a",
                        "revision": 2,
                    },
                    "elements": [
                        {
                            "object": {"global_id": GUID, "class": "IfcMember"},
                            "geometry_family": "constant_profile_extrusion",
                            "geometry_signature": SIGNATURE,
                            "measurements": [
                                {
                                    "id": "profile.web_thickness",
                                    "quantity_kind": "length",
                                    "value_si": 0.01,
                                    "source": "profile_parameter",
                                    "method": "ifc_representation",
                                    "frame": "semantic",
                                    "direction": "transverse",
                                    "confidence": "high",
                                    "alternatives": [
                                        {
                                            "value_si": 0.02,
                                            "source": "mesh_section",
                                            "method": "adaptive_section",
                                            "confidence": "high",
                                        }
                                    ],
                                }
                            ],
                            "coverage": {"extracted": ["profile.web_thickness"]},
                        }
                    ],
                },
                "meta": {},
            }

        from ifc_console.sdk import AsyncWorkbench

        monkeypatch.setattr(AsyncWorkbench, "call", fake_call)
        refused = await ops.call(
            "apply_measurement_skill",
            {"name": "refuse-conflict", "global_ids": [GUID]},
        )
        ambiguous = refused.data["results"][0]["ambiguous"][0]
        assert ambiguous["verification"]["status"] == "conflict"
        assert ambiguous["verification"]["property_proposal_allowed"] is False
        reported = await ops.call(
            "apply_measurement_skill",
            {"name": "report-conflict", "global_ids": [GUID]},
        )
        extracted = reported.data["results"][0]["extracted"][0]
        assert extracted["verification"]["status"] == "conflict_reported"
