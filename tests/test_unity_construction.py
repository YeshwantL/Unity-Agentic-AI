import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import unity_integration as unity


def valid_request(**updates):
    request = {
        "scene_path": "Assets/UnityAgent/Generated/ConstructionProbe/Probe.unity",
        "prefab_path": "Assets/UnityAgent/Generated/ConstructionProbe/Prefabs/Beacon.prefab",
        "prefab_object_name": "Beacon",
        "objects": [
            {
                "name": "Beacon",
                "primitive": "Cylinder",
                "position": [1, 2, 3],
                "scale": [1, 2, 1],
                "color": [0.2, 0.5, 0.9],
                "components": {
                    "SphereCollider": {"is_trigger": True, "radius": 1.25},
                    "Rigidbody": {"is_kinematic": True, "use_gravity": False},
                    "Light": {"intensity": 2.0},
                },
            },
            {
                "name": "Floor",
                "primitive": "Plane",
                "position": [0, 0, 0],
                "scale": [4, 1, 4],
                "color": [0.3, 0.4, 0.2, 1],
            },
        ],
        "replace_existing": False,
    }
    request.update(updates)
    return request


class ConstructionPathTests(unittest.TestCase):
    def test_accepts_paths_in_dedicated_generated_root(self):
        self.assertIsNone(unity.validate_construction_asset_path(
            "Assets/UnityAgent/Generated/ObstacleCourse/Scenes/Level.unity",
            ".unity",
        ))

    def test_rejects_absolute_and_wrong_root_paths(self):
        for path in (
            "D:/Unity/UnityAgent/Assets/UnityAgent/Generated/Level.unity",
            "Assets/Scenes/Level.unity",
            "Assets/UnityAgent/Editor/Builder.unity",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(
                    unity.validate_construction_asset_path(path, ".unity")
                )

    def test_rejects_traversal_and_noncanonical_paths(self):
        for path in (
            "Assets/UnityAgent/Generated/../Scenes/Level.unity",
            "Assets/UnityAgent/Generated/./Level.unity",
            "Assets//UnityAgent/Generated/Level.unity",
            r"Assets\UnityAgent\Generated\Level.unity",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(
                    unity.validate_construction_asset_path(path, ".unity")
                )

    def test_rejects_wrong_asset_extension(self):
        self.assertIsNotNone(unity.validate_construction_asset_path(
            "Assets/UnityAgent/Generated/Level.prefab", ".unity"
        ))


class ConstructionRequestTests(unittest.TestCase):
    def test_valid_scene_prefab_and_allowlisted_component_properties(self):
        result = unity.validate_construction_request(valid_request())

        self.assertTrue(result["success"], result.get("error"))
        beacon = result["request"]["objects"][0]
        self.assertEqual(
            {component["type"] for component in beacon["components"]},
            {"SphereCollider", "Rigidbody", "Light"},
        )
        sphere = next(
            item for item in beacon["components"]
            if item["type"] == "SphereCollider"
        )
        self.assertTrue(sphere["has_is_trigger"])
        self.assertTrue(sphere["is_trigger"])
        self.assertTrue(sphere["has_radius"])
        self.assertEqual(sphere["radius"], 1.25)

    def test_rejects_unapproved_components_and_properties(self):
        request = valid_request()
        request["objects"][0]["components"]["System.Diagnostics.Process"] = {}
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("not approved", result["error"])

        request = valid_request()
        request["objects"][0]["components"]["Light"] = {"color": [1, 0, 0]}
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("Unsupported Light properties", result["error"])

    def test_accepts_explicit_gameplay_components_and_named_references(self):
        request = valid_request()
        request["prefab_path"] = None
        request["prefab_object_name"] = None
        request["objects"] = [
            {
                "name": "Manager", "primitive": "Cube",
                "components": {
                    "CourseGameManager": {
                        "player": "Player", "required_collectibles": 3,
                    },
                },
            },
            {
                "name": "Player", "primitive": "Capsule",
                "components": {
                    "CharacterController": {"height": 2.0, "radius": 0.45},
                    "CoursePlayerController": {
                        "game_manager": "Manager", "camera": "Camera",
                    },
                },
            },
            {"name": "Camera", "primitive": "Cube"},
        ]
        validated = unity.validate_construction_request(request)

        self.assertTrue(validated["success"], validated.get("error"))
        player = next(
            item for item in validated["request"]["objects"]
            if item["name"] == "Player"
        )
        movement = next(
            item for item in player["components"]
            if item["type"] == "CoursePlayerController"
        )
        self.assertEqual(movement["game_manager_name"], "Manager")
        self.assertEqual(movement["camera_name"], "Camera")

    def test_rejects_unresolved_gameplay_reference_and_missing_required_binding(self):
        request = valid_request()
        request["objects"][0]["components"] = {
            "CourseGameManager": {"player": "Missing", "required_collectibles": 3},
        }
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("reference", result["error"])

        request["objects"][0]["components"] = {"CourseGameManager": {}}
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("missing required properties", result["error"])

    def test_rejects_invalid_values_and_unknown_primitive(self):
        request = valid_request()
        request["objects"][0]["scale"] = [1, 0, 1]
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("positive", result["error"])

        request = valid_request()
        request["objects"][0]["primitive"] = "ArbitraryPrefab"
        result = unity.validate_construction_request(request)
        self.assertFalse(result["success"])
        self.assertIn("unapproved primitive", result["error"])

    def test_invalid_request_returns_structured_failure_without_unity(self):
        result = unity.create_unity_scene(
            "Assets/Scenes/Unsafe.unity",
            [{"name": "Unsafe", "primitive": "Cube"}],
        )
        self.assertFalse(result["success"])
        self.assertIsNone(result["unity_exit_code"])
        self.assertEqual(result["created_asset_paths"], [])
        self.assertEqual(result["verification_findings"], [])
        self.assertTrue(result["errors"])

    def test_refuses_existing_scene_prefab_and_material_without_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "Assets"
            scene = assets / "UnityAgent/Generated/ConstructionProbe/Probe.unity"
            prefab = assets / "UnityAgent/Generated/ConstructionProbe/Prefabs/Beacon.prefab"
            material = assets / "UnityAgent/Generated/ConstructionProbe/Materials/Beacon_Material.mat"
            for existing in (scene, prefab, material):
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text("preserve", encoding="utf-8")
            project = {
                "success": True,
                "project_path": str(root),
                "assets_path": str(assets),
            }
            with patch.object(unity, "validate_unity_project", return_value=project):
                result = unity.create_unity_scene(**{
                    key: value
                    for key, value in {
                        "scene_path": valid_request()["scene_path"],
                        "objects": valid_request()["objects"],
                        "prefab_path": valid_request()["prefab_path"],
                        "prefab_object_name": "Beacon",
                    }.items()
                })

            self.assertFalse(result["success"])
            self.assertIn("Refusing to overwrite", result["errors"][0])
            for existing in (scene, prefab, material):
                self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")

    def test_explicit_replacement_backs_up_existing_assets_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "Assets"
            scene = assets / "UnityAgent/Generated/ConstructionProbe/Probe.unity"
            prefab = assets / "UnityAgent/Generated/ConstructionProbe/Prefabs/Beacon.prefab"
            material = assets / "UnityAgent/Generated/ConstructionProbe/Materials/Beacon_Material.mat"
            for existing in (scene, prefab, material):
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text("preserve", encoding="utf-8")
            project = {
                "success": True,
                "project_path": str(root),
                "assets_path": str(assets),
            }
            with (
                patch.object(unity, "validate_unity_project", return_value=project),
                patch.dict("os.environ", {"UNITY_EDITOR_PATH": ""}),
            ):
                result = unity.create_unity_scene(
                    scene_path=valid_request()["scene_path"],
                    objects=valid_request()["objects"],
                    prefab_path=valid_request()["prefab_path"],
                    prefab_object_name="Beacon",
                    replace_existing=True,
                )

            self.assertFalse(result["success"])
            self.assertIn("UNITY_EDITOR_PATH is not configured", result["errors"][0])
            backups = list(root.rglob("*.backup-*"))
            self.assertEqual(backups, [])

    def test_backup_helper_preserves_explicitly_replaceable_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory) / "Assets"
            targets = [
                assets / "UnityAgent/Generated/Scene.unity",
                assets / "UnityAgent/Generated/Prefabs/Item.prefab",
                assets / "UnityAgent/Generated/Materials/Item_Material.mat",
            ]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("preserve", encoding="utf-8")

            error = unity._backup_existing_construction_assets([
                (str(target.relative_to(assets)).replace("\\", "/"), target)
                for target in targets
            ])

            self.assertIsNone(error)
            backups = list(assets.rglob("*.backup-*"))
            self.assertEqual(len(backups), 3)
            self.assertEqual(
                {backup.read_text(encoding="utf-8") for backup in backups},
                {"preserve"},
            )

    def test_bridge_replacement_requires_opt_in_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "UnityAgentConstruction.cs"
            meta = Path(str(bridge) + ".meta")
            bridge.write_text("existing bridge", encoding="utf-8")
            meta.write_text("existing meta", encoding="utf-8")

            error = unity._ensure_construction_bridge(
                bridge, "updated bridge", replace_existing=False
            )
            self.assertIn("refusing to overwrite", error)
            self.assertEqual(bridge.read_text(encoding="utf-8"), "existing bridge")

            with patch.object(
                unity, "unity_create_file", return_value={"success": True}
            ) as deploy:
                error = unity._ensure_construction_bridge(
                    bridge, "updated bridge", replace_existing=True
                )

            self.assertIsNone(error)
            deploy.assert_called_once_with(
                unity.UNITY_AGENT_EDITOR_SCRIPT, "updated bridge"
            )
            backups = list(Path(directory).glob("*.backup-*"))
            self.assertEqual(len(backups), 2)
            self.assertEqual(
                {backup.read_text(encoding="utf-8") for backup in backups},
                {"existing bridge", "existing meta"},
            )

    def test_parses_structured_scene_and_prefab_verification(self):
        payload = (
            '{"success":true,"scene_path":"Assets/UnityAgent/Generated/Test/Scene.unity",'
            '"prefab_path":"Assets/UnityAgent/Generated/Test/Prefabs/Item.prefab",'
            '"created_asset_paths":["Assets/UnityAgent/Generated/Test/Materials/Item_Material.mat",'
            '"Assets/UnityAgent/Generated/Test/Prefabs/Item.prefab",'
            '"Assets/UnityAgent/Generated/Test/Scene.unity"],'
            '"verification_findings":[{"object_name":"Item","exists_after_reload":true,'
            '"verified_components":["Renderer","Collider","SphereCollider"],"error":null}],'
            '"errors":[]}'
        )
        report = unity._parse_construction_report(
            "Unity log " + unity.UNITY_AGENT_RESULT_MARKER + payload
        )

        self.assertIsNotNone(report)
        self.assertTrue(report["success"])
        self.assertEqual(len(report["created_asset_paths"]), 3)
        self.assertTrue(report["verification_findings"][0]["exists_after_reload"])
        self.assertIsNone(unity._parse_construction_report("no report marker"))

    def test_malformed_structured_report_fails_closed(self):
        report = unity._parse_construction_report(
            unity.UNITY_AGENT_RESULT_MARKER + "{bad json"
        )
        self.assertFalse(report["success"])
        self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
