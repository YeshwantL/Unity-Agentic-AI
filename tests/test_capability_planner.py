import unittest

from agent.capabilities import CAPABILITY_REGISTRY, build_capability_plan
from agent.main import ProjectPlan, get_available_tasks


class CapabilityPlannerTests(unittest.TestCase):
    def test_collectible_obstacle_course_selects_matching_systems(self):
        idea = (
            "Build a small 3D collectible obstacle-course game. A player runs and jumps "
            "over obstacles, collects all gems, and reaches the finish."
        )
        plan = ProjectPlan.model_validate(build_capability_plan(idea))

        self.assertEqual(plan.genre, "3D collectible obstacle course")
        self.assertEqual(plan.camera_perspective, "third-person")
        self.assertIn("movement.third_person", plan.selected_capabilities)
        self.assertIn("camera.control", plan.selected_capabilities)
        self.assertIn("collectibles.counter", plan.selected_capabilities)
        self.assertIn("objectives.collect_all", plan.selected_capabilities)
        self.assertIn("ui.hud", plan.selected_capabilities)
        self.assertNotIn("enemy.basic", plan.selected_capabilities)
        self.assertNotIn("spawning.waves", plan.selected_capabilities)
        task_by_capability = {
            task.capability_id: task
            for phase in plan.phases
            for task in phase.tasks
        }
        assembly = task_by_capability["construction.assemble"]
        self.assertEqual(
            set(assembly.dependencies),
            {task.id for capability, task in task_by_capability.items()
             if capability != "construction.assemble"},
        )
        self.assertEqual(
            assembly.inputs["scene_path"],
            "Assets/UnityAgent/Generated/ASmall3DCollectibleObstacleCourseGame/Scenes/Main.unity",
        )
        self.assertTrue(assembly.acceptance_criteria)

    def test_task_dependencies_match_capability_prerequisites(self):
        plan = ProjectPlan.model_validate(build_capability_plan(
            "A first-person game where the player shoots enemies and collects items."
        ))
        tasks = {
            task.capability_id: task
            for phase in plan.phases
            for task in phase.tasks
        }
        self.assertEqual(
            set(tasks["enemy.basic"].dependencies),
            {next(task.id for phase in plan.phases for task in phase.tasks
                  if task.capability_id == "scene.primitives"),
             next(task.id for phase in plan.phases for task in phase.tasks
                  if task.capability_id == "player.health")},
        )
        for capability_id, task in tasks.items():
            required_ids = {
                tasks[prerequisite].id
                for prerequisite in CAPABILITY_REGISTRY[capability_id].prerequisites
                if prerequisite in tasks
            }
            self.assertTrue(required_ids.issubset(task.dependencies))
        camera_task = tasks["camera.control"]
        movement_task = tasks["movement.first_person"]
        self.assertIn(movement_task.id, camera_task.dependencies)

    def test_dependencies_are_ordered_before_dependent_tasks(self):
        plan = ProjectPlan.model_validate(build_capability_plan(
            "A third-person obstacle course: collect every coin and reach the exit."
        ))
        ordered_ids = [
            task.id
            for phase in plan.phases
            for task in phase.tasks
        ]
        positions = {task_id: index for index, task_id in enumerate(ordered_ids)}
        for phase in plan.phases:
            for task in phase.tasks:
                for dependency_id in task.dependencies:
                    self.assertLess(positions[dependency_id], positions[task.id])

    def test_langgraph_ready_task_resolver_blocks_objective_until_collectibles_complete(self):
        plan = ProjectPlan.model_validate(build_capability_plan(
            "A third-person obstacle course: collect every coin and reach the exit."
        ))
        available = get_available_tasks(plan, completed_tasks=[])
        available_capabilities = {task.capability_id for task in available}

        self.assertIn("scene.primitives", available_capabilities)
        self.assertNotIn("objectives.collect_all", available_capabilities)

        task_by_capability = {
            task.capability_id: task
            for phase in plan.phases
            for task in phase.tasks
        }
        scene_task_id = task_by_capability["scene.primitives"].id
        collectible_task_id = task_by_capability["collectibles.counter"].id
        after_scene = get_available_tasks(plan, completed_tasks=[scene_task_id])
        self.assertIn(
            "collectibles.counter",
            {task.capability_id for task in after_scene},
        )
        self.assertNotIn(
            "objectives.collect_all",
            {task.capability_id for task in after_scene},
        )
        after_collection = get_available_tasks(
            plan,
            completed_tasks=[scene_task_id, collectible_task_id],
        )
        self.assertIn(
            "objectives.collect_all",
            {task.capability_id for task in after_collection},
        )

    def test_plan_records_inferred_details_and_uncertainties(self):
        plan = ProjectPlan.model_validate(build_capability_plan(
            "Create a 2D platformer with a countdown timer and checkpoints."
        ))
        self.assertEqual(plan.camera_perspective, "2D")
        self.assertIn("movement.simple_2d", plan.selected_capabilities)
        self.assertIn("objectives.timer", plan.selected_capabilities)
        self.assertIn("checkpoints.transitions", plan.selected_capabilities)
        self.assertTrue(plan.core_loop)
        self.assertTrue(plan.controls)
        self.assertTrue(plan.game_objects)
        self.assertTrue(plan.scene_requirements)
        self.assertTrue(plan.acceptance_criteria)
        self.assertTrue(plan.missing_information)

    def test_empty_game_idea_is_rejected(self):
        with self.assertRaises(ValueError):
            build_capability_plan("  ")


if __name__ == "__main__":
    unittest.main()
