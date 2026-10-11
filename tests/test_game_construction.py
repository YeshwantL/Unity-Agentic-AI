import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.game_construction import (
    CAPABILITY_TASK_EXECUTORS,
    _game_runtime_source,
    _scene_test_assembly,
    execute_capability_task,
)
from agent.unity_integration import unity_generated_game_namespace


class CapabilityExecutorTests(unittest.TestCase):
    def test_executor_registry_is_bounded_and_explicit(self):
        self.assertEqual(
            set(CAPABILITY_TASK_EXECUTORS),
            {
                "scene.primitives", "movement.third_person", "camera.control",
                "collectibles.counter", "objectives.collect_all",
                "game_state.core", "ui.hud", "construction.assemble",
            },
        )

    def test_unknown_capability_fails_with_diagnostic(self):
        task = SimpleNamespace(
            id="CAP.unsupported", capability_id="unregistered.capability",
            inputs={"capability_id": "unregistered.capability"}, dependencies=[],
        )

        result = execute_capability_task(
            {"completed_tasks": []}, task, "unregistered.capability"
        )

        self.assertFalse(result["success"])
        self.assertIn("No bounded executor", result["error"])

    def test_task_cannot_execute_before_declared_dependencies(self):
        task = SimpleNamespace(
            id="CAP.camera", capability_id="camera.control",
            inputs={
                "capability_id": "camera.control",
                "scene_path": "Assets/UnityAgent/Generated/Game/Scenes/Main.unity",
            },
            dependencies=["CAP.movement"],
        )

        result = execute_capability_task({"completed_tasks": []}, task, "camera.control")

        self.assertFalse(result["success"])
        self.assertIn("CAP.movement", result["error"])

    def test_test_assembly_names_are_unique_per_generated_project(self):
        first = _scene_test_assembly(
            "Assets/UnityAgent/Generated/ASmall3DCollectibleObstacleCourseGame"
        )
        second = _scene_test_assembly(
            "Assets/UnityAgent/Generated/AThirdPerson3DGemTrailObstacleCourse"
        )

        self.assertNotEqual(first, second)
        self.assertEqual(
            first,
            "CollectibleCoursePlayMode_ASmall3DCollectibleObstacleCourseGame",
        )

    def test_generated_runtime_namespaces_are_safe_and_project_scoped(self):
        root = "Assets/UnityAgent/Generated/AThird-PersonCourse"

        namespace = unity_generated_game_namespace(root)
        source = _game_runtime_source("public sealed class Sample {}", root)

        self.assertEqual(namespace, "UnityAgent.Generated.AThird_PersonCourse")
        self.assertTrue(source.startswith(f"namespace {namespace}\n{{\n"))
        self.assertTrue(source.endswith("}\n"))
        self.assertIsNone(unity_generated_game_namespace("Assets/../Outside"))

    def test_scene_specification_asset_is_read_back_before_task_success(self):
        task = SimpleNamespace(
            id="CAP.scene", capability_id="scene.primitives",
            inputs={
                "capability_id": "scene.primitives",
                "scene_path": "Assets/UnityAgent/Generated/Game/Scenes/Main.unity",
            },
            dependencies=[],
        )
        plan = SimpleNamespace(
            project_name="Game", genre="test", camera_perspective="third-person",
            core_loop="move and collect", selected_capabilities=["scene.primitives"],
            scene_requirements=["ground"], acceptance_criteria=["scene exists"],
        )
        with patch(
            "agent.game_construction.unity_create_file",
            return_value={"success": True, "bytes_written": 20},
        ), patch(
            "agent.game_construction.unity_read_file",
            side_effect=[
                {"success": False, "content": None},
                {"success": True, "content": None},
            ],
        ):
            result = execute_capability_task(
                {"completed_tasks": [], "plan": plan}, task, "scene.primitives"
            )

        self.assertFalse(result["success"])
        self.assertIn("read-back verification failed", result["error"])


if __name__ == "__main__":
    unittest.main()
