"""Bounded executors for capability-driven Unity construction tasks."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    from .unity_integration import (
        create_unity_scene,
        run_unity_playmode_test,
        unity_create_file,
        unity_generated_game_namespace,
        unity_read_file,
        validate_unity_compilation,
    )
except ImportError:
    from unity_integration import (
        create_unity_scene,
        run_unity_playmode_test,
        unity_create_file,
        unity_generated_game_namespace,
        unity_read_file,
        validate_unity_compilation,
    )


ASSET_ROOT = "Assets/UnityAgent/Generated"
SCENE_TEST_ASSEMBLY_PREFIX = "CollectibleCoursePlayMode"
SCENE_SOURCE_PLACEHOLDER = "__GENERATED_SCENE_PATH__"
CAPABILITY_SOURCE_FILES = {
    "movement.third_person": ("CoursePlayerController.cs",),
    "camera.control": ("CourseCameraFollow.cs",),
    "collectibles.counter": ("CourseCollectible.cs",),
    "objectives.collect_all": ("CourseFinish.cs",),
    "game_state.core": ("CourseGameManager.cs",),
    "ui.hud": ("CourseHud.cs",),
}
SUPPORTED_GAME_CAPABILITIES = frozenset({
    "scene.primitives",
    *CAPABILITY_SOURCE_FILES,
    "construction.assemble",
})
GAME_OBJECTS = [
    {
        "name": "Ground", "primitive": "Cube", "position": [0, -0.5, 30],
        "scale": [12, 1, 80], "color": [0.25, 0.55, 0.28],
    },
    {
        "name": "Player", "primitive": "Capsule", "position": [0, 1, 0],
        "scale": [1, 1, 1], "color": [0.2, 0.45, 0.95],
        "components": {
            "CharacterController": {
                "height": 2.0, "radius": 0.45, "slope_limit": 45.0,
                "step_offset": 0.3,
            },
            "CoursePlayerController": {
                "game_manager": "Course Manager", "camera": "Main Camera",
                "move_speed": 6.0, "jump_height": 1.4,
            },
        },
    },
    {
        "name": "Course Manager", "primitive": "Cube", "position": [-5, 1, 0],
        "scale": [0.4, 0.4, 0.4], "color": [0.2, 0.2, 0.2],
        "components": {"CourseGameManager": {
            "player": "Player", "required_collectibles": 3,
        }},
    },
    {
        "name": "Main Camera", "primitive": "Cube", "position": [0, 7, -8],
        "scale": [0.1, 0.1, 0.1], "color": [0, 0, 0],
        "components": {
            "Camera": {"field_of_view": 60.0},
            "CourseCameraFollow": {"target": "Player"},
        },
    },
    {
        "name": "Course HUD", "primitive": "Cube", "position": [-5, 2, 0],
        "scale": [0.1, 0.1, 0.1], "color": [0, 0, 0],
        "components": {"CourseHud": {"game_manager": "Course Manager"}},
    },
    {
        "name": "Sun", "primitive": "Sphere", "position": [0, 8, 12],
        "scale": [0.2, 0.2, 0.2], "color": [1, 0.9, 0.65],
        "components": {"Light": {"intensity": 2.0}},
    },
    {
        "name": "Obstacle A", "primitive": "Cube", "position": [0, 0.65, 9],
        "scale": [1.2, 1.3, 1], "color": [0.72, 0.3, 0.22],
    },
    {
        "name": "Obstacle B", "primitive": "Cube", "position": [0, 0.65, 19],
        "scale": [1.2, 1.3, 1], "color": [0.72, 0.3, 0.22],
    },
    {
        "name": "Obstacle C", "primitive": "Cube", "position": [0, 0.65, 29],
        "scale": [1.2, 1.3, 1], "color": [0.72, 0.3, 0.22],
    },
    *[
        {
            "name": f"Gem {index}", "primitive": "Sphere",
            "position": [x, 0.8, z], "scale": [0.7, 0.7, 0.7],
            "color": [0.2, 0.9, 1.0],
            "components": {
                "SphereCollider": {"is_trigger": True, "radius": 0.75},
                "Rigidbody": {"is_kinematic": True, "use_gravity": False},
                "CourseCollectible": {"game_manager": "Course Manager"},
            },
        }
        for index, (x, z) in enumerate(((-2, 5), (2, 16), (-2, 27)), start=1)
    ],
    {
        "name": "Finish", "primitive": "Cube", "position": [0, 1, 39],
        "scale": [4, 2, 1], "color": [0.95, 0.75, 0.15],
        "components": {
            "BoxCollider": {"is_trigger": True},
            "CourseFinish": {"game_manager": "Course Manager", "player": "Player"},
        },
    },
]


def _asset_root_from_scene(scene_path: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(
        r"Assets/UnityAgent/Generated/([A-Za-z0-9_-]+)/Scenes/Main\.unity",
        scene_path,
    )
    if not match:
        return None, "Assembly scene path must use the generated project's canonical Scenes/Main.unity location."
    return f"{ASSET_ROOT}/{match.group(1)}", None


def _scene_test_assembly(root: str) -> str:
    project_name = root.rsplit("/", 1)[-1]
    safe_project_name = re.sub(r"[^A-Za-z0-9_]", "_", project_name)
    return f"{SCENE_TEST_ASSEMBLY_PREFIX}_{safe_project_name}"


def _game_runtime_source(source: str, root: str) -> str:
    namespace = unity_generated_game_namespace(root)
    if namespace is None:
        raise ValueError("Generated game asset root does not have a valid namespace.")
    return f"namespace {namespace}\n{{\n{source.rstrip()}\n}}\n"


def _write_new_asset(
    relative_path: str,
    content: str,
    expected_previous_content: str | None = None,
) -> dict[str, Any]:
    pure_path = PurePosixPath(relative_path)
    if (pure_path.is_absolute() or "\\" in relative_path or ".." in pure_path.parts
            or not relative_path.startswith(ASSET_ROOT + "/")):
        return {"success": False, "path": relative_path, "error": "Generated asset path is not permitted."}

    existing = unity_read_file(relative_path)
    if existing.get("success"):
        if existing.get("content") == content:
            return {"success": True, "path": relative_path, "already_matches": True}
        if (expected_previous_content is not None
                and existing.get("content") == expected_previous_content):
            backup_path = relative_path + ".backup-" + datetime.now(
                timezone.utc
            ).strftime("%Y%m%dT%H%M%S%fZ")
            backup = unity_create_file(backup_path, expected_previous_content)
            if not backup.get("success"):
                return {
                    "success": False, "path": relative_path,
                    "error": f"Could not preserve the prior generated asset: {backup.get('error')}",
                }
            backup_readback = unity_read_file(backup_path)
            if (not backup_readback.get("success")
                    or backup_readback.get("content") != expected_previous_content):
                return {
                    "success": False, "path": relative_path,
                    "error": "Prior generated asset backup failed read-back verification.",
                }
            replaced = unity_create_file(relative_path, content)
            if not replaced.get("success"):
                return {
                    "success": False, "path": relative_path,
                    "error": f"Could not update the verified prior generated asset: {replaced.get('error')}",
                }
            updated_readback = unity_read_file(relative_path)
            if not updated_readback.get("success") or updated_readback.get("content") != content:
                return {
                    "success": False, "path": relative_path,
                    "error": f"Updated generated asset failed read-back verification: {relative_path}",
                }
            return {
                "success": True, "path": relative_path,
                "backup_path": backup_path, "verified": True,
            }
        return {
            "success": False, "path": relative_path,
            "error": f"Refusing to overwrite existing generated asset: {relative_path}",
        }

    created = unity_create_file(relative_path, content)
    if not created.get("success"):
        return {"success": False, "path": relative_path, "error": created.get("error")}
    readback = unity_read_file(relative_path)
    if not readback.get("success") or readback.get("content") != content:
        return {
            "success": False, "path": relative_path,
            "error": f"Generated asset read-back verification failed: {relative_path}",
        }
    return {
        "success": True, "path": relative_path,
        "bytes_written": created.get("bytes_written", 0),
        "verified": True,
    }


def _scene_path_from_task(task: Any) -> tuple[str | None, str | None]:
    inputs = getattr(task, "inputs", {}) or {}
    scene_path = inputs.get("scene_path")
    if not isinstance(scene_path, str):
        return None, "Assembly task is missing its scene_path input."
    _, error = _asset_root_from_scene(scene_path)
    return (None, error) if error else (scene_path, None)


def execute_capability_task(state: dict[str, Any], task: Any, capability_id: str) -> dict[str, Any]:
    task_inputs = getattr(task, "inputs", {}) or {}
    if task_inputs.get("capability_id") != capability_id:
        return {
            "success": False, "task_id": task.id,
            "error": f"Task inputs do not match capability '{capability_id}'.",
            "tool_results": [], "verified": False,
        }
    unfinished = sorted(set(task.dependencies) - set(state.get("completed_tasks", [])))
    if unfinished:
        return {
            "success": False, "task_id": task.id,
            "error": f"Capability prerequisites are not complete: {unfinished}",
            "tool_results": [], "verified": False,
        }
    if capability_id not in SUPPORTED_GAME_CAPABILITIES:
        return {
            "success": False, "task_id": task.id,
            "error": f"No bounded executor supports capability '{capability_id}'.",
            "tool_results": [], "verified": False,
        }

    scene_path, path_error = _scene_path_from_task(task)
    if path_error:
        return {
            "success": False, "task_id": task.id, "error": path_error,
            "tool_results": [], "verified": False,
        }
    root, root_error = _asset_root_from_scene(scene_path)
    if root_error:
        return {
            "success": False, "task_id": task.id, "error": root_error,
            "tool_results": [], "verified": False,
        }

    if capability_id == "construction.assemble":
        return _assemble_and_validate(state, task, scene_path, root)

    asset_results = []
    if capability_id == "scene.primitives":
        plan = state["plan"]
        scene_spec = {
            "project_name": plan.project_name,
            "genre": plan.genre,
            "camera_perspective": plan.camera_perspective,
            "core_loop": plan.core_loop,
            "selected_capabilities": plan.selected_capabilities,
            "scene_requirements": plan.scene_requirements,
            "acceptance_criteria": plan.acceptance_criteria,
            "scene_path": scene_path,
        }
        asset_results.append(_write_new_asset(
            f"{root}/SceneSpecification.json",
            json.dumps(scene_spec, indent=2, ensure_ascii=False) + "\n",
        ))

    source_directory = Path(__file__).with_name("game_assets")
    for file_name in CAPABILITY_SOURCE_FILES.get(capability_id, ()):
        source_path = source_directory / file_name
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "success": False, "task_id": task.id,
                "error": f"Capability source asset is unavailable: {exc}",
                "tool_results": asset_results, "verified": False,
            }
        asset_results.append(_write_new_asset(
            f"{root}/Scripts/{file_name}",
            _game_runtime_source(source, root),
            expected_previous_content=source,
        ))

    succeeded = bool(asset_results) and all(item.get("success") for item in asset_results)
    return {
        "success": succeeded,
        "task_id": task.id,
        "capability_id": capability_id,
        "files_deployed": [item["path"] for item in asset_results if item.get("success")],
        "files_verified": [
            item["path"] for item in asset_results
            if item.get("success") and (item.get("verified") or item.get("already_matches"))
        ],
        "tool_results": asset_results,
        "verified": succeeded and all(
            item.get("verified") or item.get("already_matches") for item in asset_results
        ),
        "error": next((item.get("error") for item in asset_results if not item.get("success")), None),
    }


def _assemble_and_validate(
    state: dict[str, Any], task: Any, scene_path: str, root: str
) -> dict[str, Any]:
    plan = state["plan"]
    selected = set(plan.selected_capabilities)
    unsupported = sorted(selected - SUPPORTED_GAME_CAPABILITIES)
    required = {
        "scene.primitives", "movement.third_person", "camera.control",
        "collectibles.counter", "objectives.collect_all", "game_state.core",
        "ui.hud", "construction.assemble",
    }
    missing = sorted(required - selected)
    if unsupported or missing:
        return {
            "success": False, "task_id": task.id,
            "error": (
                f"Bounded construction does not support the selected capability set; "
                f"unsupported={unsupported}, missing={missing}."
            ),
            "tool_results": [], "verified": False,
        }

    source_directory = Path(__file__).with_name("game_assets")
    deployed_assets: list[str] = []
    for capability_id, file_names in CAPABILITY_SOURCE_FILES.items():
        if capability_id not in selected:
            continue
        for file_name in file_names:
            path = f"{root}/Scripts/{file_name}"
            source = (source_directory / file_name).read_text(encoding="utf-8")
            deployed_source = _game_runtime_source(source, root)
            readback = unity_read_file(path)
            if not readback.get("success") or readback.get("content") != deployed_source:
                return {
                    "success": False, "task_id": task.id,
                    "error": f"Required capability source is missing or differs: {path}",
                    "files_deployed": deployed_assets, "tool_results": [],
                    "verified": False,
                }
            deployed_assets.append(path)

    test_assembly = _scene_test_assembly(root)
    asmdef_path = f"{root}/Tests/Editor/{SCENE_TEST_ASSEMBLY_PREFIX}.asmdef"
    test_path = f"{root}/Tests/Editor/CollectibleCoursePlayModeTests.cs"
    source_asmdef = (
        source_directory / f"{SCENE_TEST_ASSEMBLY_PREFIX}.asmdef"
    ).read_text(encoding="utf-8")
    test_asmdef_data = json.loads(source_asmdef)
    test_asmdef_data["name"] = test_assembly
    test_asmdef = json.dumps(test_asmdef_data, indent=2) + "\n"
    test_source = (source_directory / "CollectibleCoursePlayModeTests.cs").read_text(encoding="utf-8")
    if test_source.count(SCENE_SOURCE_PLACEHOLDER) != 1:
        return {
            "success": False, "task_id": task.id,
            "error": "The PlayMode test scene placeholder is missing or ambiguous.",
            "tool_results": [], "verified": False,
        }
    legacy_test_source = test_source.replace(
        "UnityEngine.Object.FindObjectsByType<MonoBehaviour>",
        "FindObjectsByType<MonoBehaviour>",
    ).replace(
        'Invoke(gems[1], "TryCollect", player);',
        'Invoke(gems[1], "SendMessage", "OnTriggerEnter", playerCollider);',
    ).replace(
        'Invoke(gems[2], "TryCollect", player);',
        'Invoke(gems[2], "SendMessage", "OnTriggerEnter", playerCollider);',
    )
    legacy_test_source = legacy_test_source.replace(SCENE_SOURCE_PLACEHOLDER, scene_path)
    test_source = test_source.replace(SCENE_SOURCE_PLACEHOLDER, scene_path)
    test_assets = [
        _write_new_asset(
            asmdef_path, test_asmdef, expected_previous_content=source_asmdef,
        ),
        _write_new_asset(
            test_path, test_source,
            expected_previous_content=legacy_test_source,
        ),
    ]
    if not all(item.get("success") for item in test_assets):
        return {
            "success": False, "task_id": task.id,
            "error": next(item.get("error") for item in test_assets if not item.get("success")),
            "files_deployed": deployed_assets,
            "tool_results": test_assets, "verified": False,
        }
    deployed_assets.extend(item["path"] for item in test_assets)

    construction = create_unity_scene(
        scene_path=scene_path,
        objects=GAME_OBJECTS,
        replace_existing=False,
        replace_bridge=True,
    )
    if not construction.get("success"):
        return {
            "success": False, "task_id": task.id,
            "error": "Unity scene construction failed: " + "; ".join(construction.get("errors", [])),
            "files_deployed": deployed_assets,
            "tool_results": [{"construction": construction}],
            "verified": False,
        }

    compilation = validate_unity_compilation()
    if not compilation.get("success"):
        return {
            "success": False, "task_id": task.id,
            "error": "Unity compilation failed for the generated game.",
            "files_deployed": deployed_assets + construction.get("created_asset_paths", []),
            "tool_results": [{
                "construction": construction, "compilation": compilation,
            }],
            "verified": False,
        }

    playmode = run_unity_playmode_test(test_assembly)
    success = bool(playmode.get("success"))
    return {
        "success": success,
        "task_id": task.id,
        "capability_id": "construction.assemble",
        "files_deployed": deployed_assets + construction.get("created_asset_paths", []),
        "files_verified": deployed_assets + construction.get("created_asset_paths", []),
        "construction": construction,
        "compilation": compilation,
        "playmode": playmode,
        "exit_code": playmode.get("exit_code"),
        "compiler_errors": compilation.get("compiler_errors", [])
            + playmode.get("compiler_errors", []),
        "test_failures": playmode.get("test_failures", []),
        "runtime_errors": playmode.get("runtime_errors", []),
        "tool_results": [{
            "construction": construction,
            "compilation": compilation,
            "playmode": playmode,
        }],
        "verified": success,
        "error": None if success else "The collectible course PlayMode acceptance tests failed.",
    }


def _executor_for(capability_id: str) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    def execute(state: dict[str, Any], task: Any) -> dict[str, Any]:
        return execute_capability_task(state, task, capability_id)
    return execute


CAPABILITY_TASK_EXECUTORS: dict[
    str, Callable[[dict[str, Any], Any], dict[str, Any]]
] = {
    capability_id: _executor_for(capability_id)
    for capability_id in SUPPORTED_GAME_CAPABILITIES
}
