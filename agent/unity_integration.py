"""Explicit, path-bounded tools for deploying and validating Unity scripts."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


UNITY_AGENT_ASSET_ROOT = "Assets/UnityAgent/Generated"
UNITY_AGENT_EDITOR_SCRIPT = "Assets/UnityAgent/Editor/UnityAgentConstruction.cs"
UNITY_AGENT_CONSTRUCTION_METHOD = "UnityAgent.Editor.UnityAgentConstruction.Run"
UNITY_AGENT_RESULT_MARKER = "UNITY_AGENT_CONSTRUCTION_RESULT:"

CONSTRUCTION_COMPONENT_PROPERTIES: dict[str, frozenset[str]] = {
    "BoxCollider": frozenset({"is_trigger"}),
    "SphereCollider": frozenset({"is_trigger", "radius"}),
    "CapsuleCollider": frozenset({"is_trigger", "radius", "height"}),
    "Rigidbody": frozenset({"mass", "use_gravity", "is_kinematic"}),
    "Camera": frozenset({"field_of_view"}),
    "Light": frozenset({"intensity"}),
    "CharacterController": frozenset(
        {"height", "radius", "slope_limit", "step_offset"}
    ),
    "CourseGameManager": frozenset({"player", "required_collectibles"}),
    "CoursePlayerController": frozenset(
        {"game_manager", "camera", "move_speed", "jump_height"}
    ),
    "CourseCameraFollow": frozenset({"target"}),
    "CourseCollectible": frozenset({"game_manager"}),
    "CourseFinish": frozenset({"game_manager", "player"}),
    "CourseHud": frozenset({"game_manager"}),
}
CONSTRUCTION_REFERENCE_PROPERTIES = frozenset(
    {"player", "camera", "target", "game_manager"}
)
CONSTRUCTION_PRIMITIVES = frozenset(
    {"Cube", "Sphere", "Capsule", "Cylinder", "Plane", "Quad"}
)


COMPILER_ERROR_PATTERN = re.compile(
    r"(?P<file>.+?\.cs)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"error\s+(?P<code>CS\d+):\s*(?P<message>.*)",
    re.IGNORECASE,
)


def validate_unity_project() -> dict[str, Any]:
    """Validate the project named by configuration, never by a task argument."""
    configured_path = os.getenv("UNITY_PROJECT_PATH")
    if not configured_path:
        return {
            "success": False,
            "is_unity_project": False,
            "error": "UNITY_PROJECT_PATH is not configured.",
        }

    root = Path(configured_path).expanduser().resolve()
    if not root.is_dir():
        return {
            "success": False,
            "is_unity_project": False,
            "project_path": str(root),
            "error": "Configured Unity project directory does not exist.",
        }

    required = ("Assets", "Packages", "ProjectSettings")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        return {
            "success": False,
            "is_unity_project": False,
            "project_path": str(root),
            "error": "Missing required Unity project folders: " + ", ".join(missing),
        }

    warnings = []
    if not (root / "ProjectSettings" / "ProjectVersion.txt").is_file():
        warnings.append("ProjectSettings/ProjectVersion.txt is missing.")
    return {
        "success": True,
        "is_unity_project": True,
        "project_path": str(root),
        "assets_path": str(root / "Assets"),
        "scripts_path": str(root / "Assets" / "Scripts"),
        "warnings": warnings,
    }


def _resolve_unity_path(relative_path: str) -> tuple[Path | None, Path | None, str | None]:
    validation = validate_unity_project()
    if not validation["success"]:
        return None, None, validation["error"]

    requested = Path(relative_path)
    if requested.is_absolute():
        return None, None, "Absolute paths are not accepted; use a project-relative path."

    root = Path(validation["project_path"]).resolve()
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, root, "Path is outside the configured Unity project."
    return candidate, root, None


def unity_read_file(relative_path: str) -> dict[str, Any]:
    target, _, error = _resolve_unity_path(relative_path)
    if error:
        return {"success": False, "path": relative_path, "content": None, "error": error}
    try:
        return {
            "success": True,
            "path": relative_path,
            "content": target.read_text(encoding="utf-8"),
            "error": None,
        }
    except OSError as exc:
        return {"success": False, "path": relative_path, "content": None, "error": str(exc)}


def unity_file_exists(relative_path: str) -> dict[str, Any]:
    target, _, error = _resolve_unity_path(relative_path)
    if error:
        return {"success": False, "path": relative_path, "exists": False, "error": error}
    return {
        "success": True,
        "path": relative_path,
        "exists": target.is_file(),
        "error": None,
    }


def unity_enable_legacy_input_compatibility() -> dict[str, Any]:
    """Enable Unity's Both input mode only when the existing scripts require legacy Input APIs."""
    relative_path = "ProjectSettings/ProjectSettings.asset"
    target, _, error = _resolve_unity_path(relative_path)
    if error:
        return {"success": False, "path": relative_path, "error": error}
    try:
        content = target.read_bytes().decode("utf-8")
    except OSError as exc:
        return {"success": False, "path": relative_path, "error": str(exc)}
    pattern = re.compile(r"(?m)^(\s*activeInputHandler:\s*)(\d+)(\s*)$")
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        return {"success": False, "path": relative_path,
                "error": f"Expected one activeInputHandler setting, found {len(matches)}."}
    old_value = int(matches[0].group(2))
    if old_value not in (0, 1, 2):
        return {"success": False, "path": relative_path,
                "error": f"Unexpected activeInputHandler value: {old_value}."}
    new_value = 2 if old_value == 1 else old_value
    updated = pattern.sub(lambda match: match.group(1) + str(new_value) + match.group(3), content, count=1)
    if updated != content:
        try:
            target.write_bytes(updated.encode("utf-8"))
        except OSError as exc:
            return {"success": False, "path": relative_path, "error": str(exc)}
    return {"success": True, "path": relative_path, "old_value": old_value,
            "new_value": new_value, "changed": updated != content, "error": None}


def unity_create_file(relative_path: str, content: str) -> dict[str, Any]:
    """Write UTF-8 text only inside the configured project's Assets directory."""
    target, root, error = _resolve_unity_path(relative_path)
    if error:
        return {"success": False, "path": relative_path, "bytes_written": 0, "error": error}

    assets_root = (root / "Assets").resolve()
    try:
        target.relative_to(assets_root)
    except ValueError:
        return {
            "success": False,
            "path": relative_path,
            "bytes_written": 0,
            "error": "Unity deployment is restricted to paths inside Assets/.",
        }

    encoded = content.encode("utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        return {
            "success": True,
            "path": relative_path,
            "bytes_written": len(encoded),
            "error": None,
        }
    except OSError as exc:
        return {"success": False, "path": relative_path, "bytes_written": 0, "error": str(exc)}


def unity_delete_file(relative_path: str) -> dict[str, Any]:
    """Delete one explicitly named file inside Assets; directory deletion is never allowed."""
    target, root, error = _resolve_unity_path(relative_path)
    if error:
        return {"success": False, "path": relative_path, "deleted": False, "error": error}
    assets_root = (root / "Assets").resolve()
    try:
        target.relative_to(assets_root)
    except ValueError:
        return {"success": False, "path": relative_path, "deleted": False,
                "error": "Unity deletion is restricted to individual files inside Assets/."}
    if not target.is_file():
        return {"success": True, "path": relative_path, "deleted": False, "error": None}
    try:
        target.unlink()
        return {"success": True, "path": relative_path, "deleted": True, "error": None}
    except OSError as exc:
        return {"success": False, "path": relative_path, "deleted": False, "error": str(exc)}


def deploy_script_to_unity(relative_path: str, content: str) -> dict[str, Any]:
    """Deploy one explicitly selected C# file; never copy a workspace tree."""
    if Path(relative_path).suffix.lower() != ".cs":
        return {
            "success": False,
            "path": relative_path,
            "bytes_written": 0,
            "error": "Only individual .cs scripts can be deployed.",
        }
    return unity_create_file(relative_path, content)


def validate_construction_asset_path(relative_path: str, extension: str) -> str | None:
    """Validate one canonical asset path within the dedicated construction root."""
    if not isinstance(relative_path, str) or not relative_path:
        return "Asset path must be a non-empty project-relative path."
    if "\\" in relative_path:
        return "Unity asset paths must use forward slashes."
    requested = Path(relative_path)
    if requested.is_absolute() or re.match(r"^[A-Za-z]:", relative_path):
        return "Absolute paths are not accepted."
    if any(part in ("", ".", "..") for part in relative_path.split("/")):
        return "Asset path traversal or non-canonical path segments are not allowed."
    prefix = UNITY_AGENT_ASSET_ROOT + "/"
    if not relative_path.startswith(prefix):
        return f"Construction assets must be under {prefix}."
    if requested.suffix.lower() != extension.lower():
        return f"Expected a {extension} asset path."
    return None


def unity_generated_game_namespace(asset_root: str) -> str | None:
    match = re.fullmatch(
        r"Assets/UnityAgent/Generated/([A-Za-z0-9_-]+)", asset_root
    )
    if not match:
        return None
    project_identifier = re.sub(r"[^A-Za-z0-9_]", "_", match.group(1))
    if project_identifier[0].isdigit():
        project_identifier = "_" + project_identifier
    return f"UnityAgent.Generated.{project_identifier}"


def _construction_number(value: Any, label: str, minimum: float | None = None) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{label} must be a finite number."
    number = float(value)
    if not (number == number and abs(number) != float("inf")):
        return None, f"{label} must be a finite number."
    if minimum is not None and number < minimum:
        return None, f"{label} must be at least {minimum}."
    return number, None


def validate_construction_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate all paths, primitive kinds, and explicit component property allowlists."""
    if not isinstance(request, dict):
        return {"success": False, "error": "Construction request must be an object."}

    scene_path = request.get("scene_path")
    error = validate_construction_asset_path(scene_path, ".unity")
    if error:
        return {"success": False, "error": error, "path": scene_path}

    prefab_path = request.get("prefab_path")
    if prefab_path is not None:
        error = validate_construction_asset_path(prefab_path, ".prefab")
        if error:
            return {"success": False, "error": error, "path": prefab_path}

    objects = request.get("objects")
    if not isinstance(objects, list) or not objects:
        return {"success": False, "error": "At least one scene object is required."}
    names: set[str] = set()
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            return {"success": False, "error": f"Object at index {index} must be an object."}
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            return {"success": False, "error": f"Object at index {index} has a missing or duplicate name."}
        names.add(name)
    normalized_objects = []
    for index, item in enumerate(objects):
        name = item.get("name")
        primitive = item.get("primitive")
        if primitive not in CONSTRUCTION_PRIMITIVES:
            return {"success": False, "error": f"Object '{name}' uses an unapproved primitive."}
        normalized = {"name": name.strip(), "primitive": primitive}

        for vector_name, expected_size, default in (
            ("position", 3, [0.0, 0.0, 0.0]),
            ("scale", 3, [1.0, 1.0, 1.0]),
            ("color", 4, [0.7, 0.7, 0.7, 1.0]),
        ):
            values = item.get(vector_name, default)
            if (not isinstance(values, list) or len(values) not in
                    ({expected_size - 1, expected_size} if vector_name == "color" else {expected_size})):
                return {"success": False, "error": f"Object '{name}' has invalid {vector_name} values."}
            if vector_name == "color" and len(values) == 3:
                values = [*values, 1.0]
            converted = []
            for component_index, value in enumerate(values):
                number, error = _construction_number(value, f"{name}.{vector_name}[{component_index}]")
                if error:
                    return {"success": False, "error": error}
                if vector_name == "color" and not 0.0 <= number <= 1.0:
                    return {"success": False, "error": f"Object '{name}' color channels must be between 0 and 1."}
                if vector_name == "scale" and number <= 0.0:
                    return {"success": False, "error": f"Object '{name}' scale values must be positive."}
                converted.append(number)
            normalized[vector_name] = converted

        components = item.get("components", {})
        if not isinstance(components, dict):
            return {"success": False, "error": f"Object '{name}' components must be an object."}
        validated_components = {}
        for component_type, properties in components.items():
            if component_type not in CONSTRUCTION_COMPONENT_PROPERTIES:
                return {"success": False, "error": f"Component '{component_type}' is not approved."}
            if not isinstance(properties, dict):
                return {"success": False, "error": f"Properties for '{component_type}' must be an object."}
            unsupported = set(properties) - CONSTRUCTION_COMPONENT_PROPERTIES[component_type]
            if unsupported:
                return {
                    "success": False,
                    "error": f"Unsupported {component_type} properties: {sorted(unsupported)}",
                }
            required_properties = {
                "CourseGameManager": {"player", "required_collectibles"},
                "CoursePlayerController": {"game_manager", "camera"},
                "CourseCameraFollow": {"target"},
                "CourseCollectible": {"game_manager"},
                "CourseFinish": {"game_manager", "player"},
                "CourseHud": {"game_manager"},
            }.get(component_type, set())
            missing_properties = required_properties - set(properties)
            if missing_properties:
                return {
                    "success": False,
                    "error": (
                        f"{component_type} is missing required properties: "
                        f"{sorted(missing_properties)}"
                    ),
                }
            typed_properties = {}
            for property_name, value in properties.items():
                if property_name in ("is_trigger", "use_gravity", "is_kinematic"):
                    if not isinstance(value, bool):
                        return {
                            "success": False,
                            "error": f"{component_type}.{property_name} must be a boolean.",
                        }
                    typed_properties[property_name] = value
                    continue
                if property_name in CONSTRUCTION_REFERENCE_PROPERTIES:
                    if not isinstance(value, str) or value not in names:
                        return {
                            "success": False,
                            "error": (
                                f"{component_type}.{property_name} must reference "
                                "a requested scene object by name."
                            ),
                        }
                    typed_properties[property_name] = value
                    continue
                if property_name == "required_collectibles":
                    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                        return {
                            "success": False,
                            "error": "CourseGameManager.required_collectibles must be a positive integer.",
                        }
                    typed_properties[property_name] = value
                    continue
                minimum = (
                    0.01
                    if property_name in (
                        "mass", "radius", "height", "field_of_view",
                        "intensity", "step_offset", "move_speed", "jump_height",
                    )
                    else 0.0 if property_name == "slope_limit" else None
                )
                number, error = _construction_number(
                    value, f"{component_type}.{property_name}", minimum=minimum
                )
                if error:
                    return {"success": False, "error": error}
                if property_name == "field_of_view" and number >= 180.0:
                    return {"success": False, "error": "Camera.field_of_view must be less than 180 degrees."}
                if property_name == "slope_limit" and number > 90.0:
                    return {"success": False, "error": "CharacterController.slope_limit must not exceed 90 degrees."}
                typed_properties[property_name] = number
            validated_components[component_type] = typed_properties
        normalized["components"] = [
            {
                "type": component_type,
                "has_is_trigger": "is_trigger" in properties,
                "is_trigger": properties.get("is_trigger", False),
                "has_radius": "radius" in properties,
                "radius": properties.get("radius", 0.5),
                "has_height": "height" in properties,
                "height": properties.get("height", 2.0),
                "has_mass": "mass" in properties,
                "mass": properties.get("mass", 1.0),
                "has_use_gravity": "use_gravity" in properties,
                "use_gravity": properties.get("use_gravity", True),
                "has_is_kinematic": "is_kinematic" in properties,
                "is_kinematic": properties.get("is_kinematic", False),
                "has_field_of_view": "field_of_view" in properties,
                "field_of_view": properties.get("field_of_view", 60.0),
                "has_intensity": "intensity" in properties,
                "intensity": properties.get("intensity", 1.0),
                "slope_limit": properties.get("slope_limit", 45.0),
                "step_offset": properties.get("step_offset", 0.3),
                "has_slope_limit": "slope_limit" in properties,
                "has_step_offset": "step_offset" in properties,
                "game_manager_name": properties.get("game_manager", ""),
                "player_name": properties.get("player", ""),
                "camera_name": properties.get("camera", ""),
                "target_name": properties.get("target", ""),
                "required_collectibles": properties.get("required_collectibles", 1),
                "has_required_collectibles": "required_collectibles" in properties,
                "move_speed": properties.get("move_speed", 6.0),
                "jump_height": properties.get("jump_height", 1.4),
                "has_move_speed": "move_speed" in properties,
                "has_jump_height": "jump_height" in properties,
            }
            for component_type, properties in validated_components.items()
        ]
        normalized_objects.append(normalized)

    prefab_object_name = request.get("prefab_object_name")
    if prefab_path is not None:
        if prefab_object_name not in names:
            return {
                "success": False,
                "error": "prefab_object_name must identify one requested scene object.",
            }
    elif prefab_object_name is not None:
        return {"success": False, "error": "prefab_object_name requires prefab_path."}

    replace_existing = request.get("replace_existing", False)
    if not isinstance(replace_existing, bool):
        return {"success": False, "error": "replace_existing must be a boolean."}
    return {
        "success": True,
        "request": {
            "scene_path": scene_path,
            "prefab_path": prefab_path,
            "prefab_object_name": prefab_object_name,
            "objects": normalized_objects,
            "replace_existing": replace_existing,
        },
        "error": None,
    }


def _construction_material_paths(scene_path: str, objects: list[dict[str, Any]]) -> list[str]:
    parent = scene_path.rsplit("/", 1)[0]
    material_directory = parent + "/Materials"
    names = {
        re.sub(r"[^A-Za-z0-9_-]+", "_", item["name"]).strip("_") or "Object"
        for item in objects
    }
    return [f"{material_directory}/{name}_Material.mat" for name in sorted(names)]


def create_unity_scene(
    scene_path: str,
    objects: list[dict[str, Any]],
    prefab_path: str | None = None,
    prefab_object_name: str | None = None,
    replace_existing: bool = False,
    timeout_seconds: int = 900,
    replace_bridge: bool = False,
) -> dict[str, Any]:
    """Build a new isolated scene and optional prefab through the allowlisted Editor bridge."""
    raw_request = {
        "scene_path": scene_path,
        "objects": objects,
        "prefab_path": prefab_path,
        "prefab_object_name": prefab_object_name,
        "replace_existing": replace_existing,
    }
    validation_result = validate_construction_request(raw_request)
    if not validation_result["success"]:
        return {
            "success": False,
            "scene_path": scene_path,
            "created_asset_paths": [],
            "verification_findings": [],
            "errors": [validation_result["error"]],
            "unity_exit_code": None,
            "compiler_errors": [],
            "unity_log": "",
        }
    request = validation_result["request"]
    project = validate_unity_project()
    if not project.get("success"):
        return {
            "success": False,
            "scene_path": scene_path,
            "created_asset_paths": [],
            "verification_findings": [],
            "errors": [project["error"]],
            "unity_exit_code": None,
            "compiler_errors": [],
            "unity_log": "",
        }
    request_paths = [scene_path]
    if prefab_path:
        request_paths.append(prefab_path)
    request_paths.extend(_construction_material_paths(scene_path, request["objects"]))
    existing_paths = []
    for relative_path in request_paths:
        target, _, path_error = _resolve_unity_path(relative_path)
        if path_error:
            return {
                "success": False, "scene_path": scene_path, "created_asset_paths": [],
                "verification_findings": [], "errors": [path_error],
                "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
            }
        try:
            target.relative_to(Path(project["assets_path"]).resolve())
        except ValueError:
            return {
                "success": False, "scene_path": scene_path, "created_asset_paths": [],
                "verification_findings": [],
                "errors": [f"Construction asset path is outside Assets/: {relative_path}"],
                "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
            }
        for existing_target in (target, Path(str(target) + ".meta")):
            if existing_target.exists():
                existing_paths.append((relative_path, existing_target))

    if existing_paths and not request["replace_existing"]:
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [],
            "errors": [
                "Refusing to overwrite existing construction assets: "
                + ", ".join(path for path, _ in existing_paths)
            ],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }

    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [], "errors": ["UNITY_EDITOR_PATH is not configured."],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [],
            "errors": [f"Configured Unity Editor executable does not exist: {editor}"],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }

    if existing_paths:
        backup_error = _backup_existing_construction_assets(existing_paths)
        if backup_error:
            return {
                "success": False, "scene_path": scene_path, "created_asset_paths": [],
                "verification_findings": [],
                "errors": [backup_error],
                "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
            }

    bridge_target, _, bridge_error = _resolve_unity_path(UNITY_AGENT_EDITOR_SCRIPT)
    if bridge_error:
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [], "errors": [bridge_error],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }
    bridge_source_path = Path(__file__).with_name("UnityAgentConstruction.cs")
    try:
        bridge_source = bridge_source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [],
            "errors": [f"Could not read the bundled Unity construction bridge: {exc}"],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }

    bridge_error = _ensure_construction_bridge(
        bridge_target, bridge_source, replace_bridge
    )
    if bridge_error:
        return {
            "success": False, "scene_path": scene_path, "created_asset_paths": [],
            "verification_findings": [], "errors": [bridge_error],
            "unity_exit_code": None, "compiler_errors": [], "unity_log": "",
        }

    result: dict[str, Any] = {
        "success": False,
        "scene_path": scene_path,
        "created_asset_paths": [],
        "verification_findings": [],
        "errors": [],
        "unity_exit_code": None,
        "compiler_errors": [],
        "stdout": "",
        "stderr": "",
        "unity_log": "",
    }
    request_directory_path: Path | None = None
    with tempfile.TemporaryDirectory(
        prefix="unity-agent-construction-", ignore_cleanup_errors=True
    ) as request_directory:
        request_directory_path = Path(request_directory)
        request_path = Path(request_directory) / "construction-request.json"
        log_path = Path(request_directory) / "construction.log"
        request_path.write_text(json_dumps_for_unity(request), encoding="utf-8")
        environment = os.environ.copy()
        environment["UNITY_AGENT_CONSTRUCTION_REQUEST"] = str(request_path)
        command = [
            str(editor), "-batchmode", "-quit", "-projectPath", project["project_path"],
            "-executeMethod", UNITY_AGENT_CONSTRUCTION_METHOD,
            "-logFile", str(log_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=environment,
            )
            result["unity_exit_code"] = completed.returncode
            result["stdout"] = completed.stdout or ""
            result["stderr"] = completed.stderr or ""
            result["unity_log"] = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.is_file() else ""
            )
            combined = "\n".join(
                (result["stdout"], result["stderr"], result["unity_log"])
            )
            result["compiler_errors"] = parse_compiler_errors(combined)
            construction_report = _parse_construction_report(combined)
            if construction_report:
                result.update(construction_report)
                if construction_report.get("success"):
                    missing_outputs = []
                    for asset_path in request_paths:
                        if validate_construction_asset_path(
                            asset_path, Path(asset_path).suffix
                        ):
                            missing_outputs.append(asset_path)
                            continue
                        target, _, verify_error = _resolve_unity_path(asset_path)
                        if verify_error or target is None or not target.is_file():
                            missing_outputs.append(asset_path)
                    if missing_outputs:
                        result["success"] = False
                        result["errors"].append(
                            "Unity reported success, but output assets are missing on disk: "
                            + ", ".join(missing_outputs)
                        )
            elif completed.returncode == 0:
                result["errors"].append(
                    "Unity exited successfully without a structured construction report."
                )
            if completed.returncode != 0:
                result["errors"].append(
                    f"Unity Editor construction exited with code {completed.returncode}."
                )
            result["success"] = (
                completed.returncode == 0
                and bool(construction_report)
                and bool(result.get("success"))
                and not result["compiler_errors"]
            )
        except subprocess.TimeoutExpired as exc:
            result["stdout"] = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            result["stderr"] = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            )
            result["unity_log"] = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.is_file() else ""
            )
            result["errors"].append(
                f"Unity Editor construction timed out after {timeout_seconds} seconds."
            )
        except OSError as exc:
            result["stderr"] = str(exc)
            result["errors"].append(f"Could not launch Unity Editor: {exc}")
    if request_directory_path and request_directory_path.exists():
        cleanup_error = None
        for attempt in range(5):
            try:
                shutil.rmtree(request_directory_path)
                cleanup_error = None
                break
            except OSError as exc:
                cleanup_error = exc
                if attempt < 4:
                    time.sleep(0.2)
        if cleanup_error:
            result["errors"].append(
                f"Could not clean Unity construction request directory: {cleanup_error}"
            )
            result["success"] = False
        if request_directory_path.exists():
            result["errors"].append(
                "Unity construction request directory remains after cleanup."
            )
            result["success"] = False
    return result


def json_dumps_for_unity(value: dict[str, Any]) -> str:
    """Serialize validated construction data as compact JSON for Unity JsonUtility."""
    import json
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _backup_existing_construction_assets(
    existing_paths: list[tuple[str, Path]],
) -> str | None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for relative_path, target in existing_paths:
        backup = target.with_name(target.name + f".backup-{timestamp}")
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            return f"Could not preserve backup for {relative_path}: {exc}"
    return None


def _ensure_construction_bridge(
    bridge_target: Path,
    bridge_source: str,
    replace_existing: bool,
) -> str | None:
    if bridge_target.is_file():
        try:
            if bridge_target.read_text(encoding="utf-8") == bridge_source:
                return None
        except OSError as exc:
            return f"Could not inspect existing Unity construction bridge: {exc}"
        if not replace_existing:
            return (
                "Existing Unity construction bridge differs; refusing to overwrite "
                f"{UNITY_AGENT_EDITOR_SCRIPT}."
            )
        existing = [(UNITY_AGENT_EDITOR_SCRIPT, bridge_target)]
        bridge_meta = Path(str(bridge_target) + ".meta")
        if bridge_meta.is_file():
            existing.append((UNITY_AGENT_EDITOR_SCRIPT + ".meta", bridge_meta))
        backup_error = _backup_existing_construction_assets(existing)
        if backup_error:
            return backup_error

    deployment = unity_create_file(UNITY_AGENT_EDITOR_SCRIPT, bridge_source)
    if not deployment.get("success"):
        return deployment.get("error", "Could not deploy the Unity construction bridge.")
    return None


def _parse_construction_report(output: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        marker_index = line.find(UNITY_AGENT_RESULT_MARKER)
        if marker_index < 0:
            continue
        payload = line[marker_index + len(UNITY_AGENT_RESULT_MARKER):].strip()
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return {
                "success": False,
                "errors": ["Unity emitted an invalid structured construction report."],
            }
        if not isinstance(parsed, dict):
            return {
                "success": False,
                "errors": ["Unity construction report must be a JSON object."],
            }
        return {
            "success": bool(parsed.get("success")),
            "created_asset_paths": parsed.get("created_asset_paths", []),
            "verification_findings": parsed.get("verification_findings", []),
            "errors": parsed.get("errors", []),
            "scene_path": parsed.get("scene_path"),
            "prefab_path": parsed.get("prefab_path"),
        }
    return None


def parse_compiler_errors(output: str) -> list[dict[str, Any]]:
    """Parse actual C# compiler diagnostics, excluding generic Unity messages."""
    errors = []
    for line in output.splitlines():
        match = COMPILER_ERROR_PATTERN.search(line)
        if match:
            errors.append({
                "code": match.group("code"),
                "file": match.group("file"),
                "line": int(match.group("line")),
                "column": int(match.group("column")),
                "message": match.group("message").strip(),
            })
    return errors


def parse_unity_messages(output: str) -> list[str]:
    """Keep generic licensing/editor errors distinct from C# compiler diagnostics."""
    messages = []
    for line in output.splitlines():
        if re.search(r"\berror\b", line, re.IGNORECASE) and not COMPILER_ERROR_PATTERN.search(line):
            messages.append(line.strip())
    return messages


def validate_unity_compilation(timeout_seconds: int = 600) -> dict[str, Any]:
    """Run only the explicitly configured Unity executable with fixed arguments."""
    validation = validate_unity_project()
    if not validation["success"]:
        return {
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "compiler_errors": [validation["error"]],
        }

    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        return {
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "compiler_errors": ["UNITY_EDITOR_PATH is not configured."],
        }
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        return {
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "compiler_errors": [f"Configured Unity Editor executable does not exist: {editor}"],
        }

    # Match the known-working PowerShell invocation and keep its log outside the project.
    with tempfile.TemporaryDirectory(prefix="unity-agent-", ignore_cleanup_errors=True) as log_directory:
        log_path = Path(log_directory) / "unity-batchmode.log"
        command = [
            str(editor), "-batchmode", "-quit", "-projectPath",
            validation["project_path"], "-logFile", str(log_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            combined_output = stdout + "\n" + stderr + "\n" + unity_log
            compiler_errors = parse_compiler_errors(combined_output)
            unity_messages = parse_unity_messages(combined_output)
            if completed.returncode != 0:
                unity_messages.append(f"Unity Editor exited with code {completed.returncode}.")
            return {
                "success": completed.returncode == 0 and not compiler_errors,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "unity_log": unity_log,
                "compiler_errors": compiler_errors,
                "unity_messages": unity_messages,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            combined_output = stdout + "\n" + stderr + "\n" + unity_log
            errors = parse_compiler_errors(combined_output)
            messages = parse_unity_messages(combined_output)
            messages.append(f"Unity compilation timed out after {timeout_seconds} seconds.")
            return {"success": False, "exit_code": None, "stdout": stdout,
                    "stderr": stderr, "unity_log": unity_log, "compiler_errors": errors,
                    "unity_messages": messages}
        except OSError as exc:
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            return {"success": False, "exit_code": None, "stdout": "", "stderr": str(exc),
                    "unity_log": unity_log,
                    "compiler_errors": [],
                    "unity_messages": [f"Could not launch Unity Editor: {exc}"]}


def _bounded_output(value: str, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return "[truncated; retained tail]\n" + value[-limit:]


def run_unity_playmode_test(assembly_name: str, timeout_seconds: int = 900) -> dict[str, Any]:
    """Run a named PlayMode test assembly and return parsed Unity evidence."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", assembly_name or ""):
        return {"success": False, "exit_code": None, "error": "Invalid PlayMode assembly name.",
                "stdout": "", "stderr": "", "unity_log": "", "compiler_errors": [],
                "runtime_errors": [], "test_failures": []}
    validation = validate_unity_project()
    if not validation["success"]:
        return {"success": False, "exit_code": None, "error": validation["error"],
                "stdout": "", "stderr": "", "unity_log": "", "compiler_errors": [],
                "runtime_errors": [], "test_failures": []}
    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        return {"success": False, "exit_code": None, "error": "UNITY_EDITOR_PATH is not configured.",
                "stdout": "", "stderr": "", "unity_log": "", "compiler_errors": [],
                "runtime_errors": [], "test_failures": []}
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        return {"success": False, "exit_code": None,
                "error": f"Configured Unity Editor executable does not exist: {editor}",
                "stdout": "", "stderr": "", "unity_log": "", "compiler_errors": [],
                "runtime_errors": [], "test_failures": []}

    with tempfile.TemporaryDirectory(prefix="unity-playmode-validation-", ignore_cleanup_errors=True) as directory:
        results_path = Path(directory) / "playmode-results.xml"
        log_path = Path(directory) / "playmode.log"
        command = [str(editor), "-batchmode", "-runTests", "-projectPath", validation["project_path"],
                   "-testPlatform", "PlayMode", "-assemblyNames", assembly_name,
                   "-testResults", str(results_path), "-logFile", str(log_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
            stdout, stderr = completed.stdout or "", completed.stderr or ""
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            combined = "\n".join((stdout, stderr, unity_log))
            xml_text = results_path.read_text(encoding="utf-8", errors="replace") if results_path.is_file() else ""
            test_failures: list[dict[str, Any]] = []
            test_total = 0
            if xml_text:
                try:
                    root = ET.fromstring(xml_text)
                    cases = [node for node in root.iter() if node.tag.split("}")[-1] == "test-case"]
                    test_total = int(root.attrib.get("total", len(cases)))
                    for case in cases:
                        if case.attrib.get("result", "").lower() == "passed":
                            continue
                        failure = next((child for child in case if child.tag.split("}")[-1] == "failure"), None)
                        test_failures.append({
                            "name": case.attrib.get("fullname", case.attrib.get("name", "unknown")),
                            "result": case.attrib.get("result", "Unknown"),
                            "message": failure.findtext("message", default="") if failure is not None else "",
                            "stack_trace": failure.findtext("stack-trace", default="") if failure is not None else "",
                        })
                except (ET.ParseError, ValueError) as exc:
                    test_failures.append({"name": "test-results.xml", "result": "Invalid XML", "message": str(exc)})
            compiler_errors = parse_compiler_errors(combined)
            runtime_errors = [line.strip() for line in combined.splitlines()
                              if re.search(r"NullReferenceException|MissingReferenceException|\bUnhandled Exception\b",
                                           line, re.IGNORECASE)]
            return {
                "success": completed.returncode == 0 and test_total > 0 and not test_failures
                    and not compiler_errors and not runtime_errors,
                "exit_code": completed.returncode, "test_total": test_total,
                "test_failures": test_failures, "compiler_errors": compiler_errors,
                "runtime_errors": runtime_errors, "stdout": _bounded_output(stdout),
                "stderr": _bounded_output(stderr), "unity_log": _bounded_output(unity_log),
                "unity_messages": parse_unity_messages(combined),
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            combined = "\n".join((stdout, stderr, unity_log))
            return {"success": False, "exit_code": None, "test_total": 0, "test_failures": [],
                    "compiler_errors": parse_compiler_errors(combined),
                    "runtime_errors": [line.strip() for line in combined.splitlines()
                        if re.search(r"NullReferenceException|MissingReferenceException|\bUnhandled Exception\b",
                                     line, re.IGNORECASE)],
                    "stdout": _bounded_output(stdout), "stderr": _bounded_output(stderr),
                    "unity_log": _bounded_output(unity_log),
                    "unity_messages": [f"Unity PlayMode validation timed out after {timeout_seconds} seconds."]}
        except OSError as exc:
            return {"success": False, "exit_code": None, "test_total": 0, "test_failures": [],
                    "compiler_errors": [], "runtime_errors": [], "stdout": "", "stderr": str(exc),
                    "unity_log": "", "unity_messages": [f"Could not launch Unity PlayMode tests: {exc}"]}


def validate_unity_files(files: dict[str, str], task_id: str = "unknown",
                         playmode_assembly: str | None = None, attempt_number: int = 1,
                         timeout_seconds: int = 600) -> dict[str, Any]:
    """Deploy explicitly named Assets files, compile, and optionally run a PlayMode assembly."""
    from datetime import datetime, timezone

    result: dict[str, Any] = {
        "task_id": task_id, "success": False, "failure_type": "unknown_error",
        "summary": "Validation did not complete.", "compiler_errors": [], "runtime_errors": [],
        "test_failures": [], "stdout": "", "stderr": "", "unity_log": "",
        "exit_code": None, "files_changed": [], "attempt_number": attempt_number,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    project = validate_unity_project()
    if not project.get("success"):
        result.update(failure_type="configuration_failed", summary=project.get("error", "Invalid Unity project."),
                      stderr=_bounded_output(project.get("stderr", "")),
                      stdout=_bounded_output(project.get("stdout", "")),
                      unity_log=_bounded_output(project.get("unity_log", "")))
        return result

    for relative_path, content in files.items():
        if not relative_path.startswith("Assets/") or Path(relative_path).is_absolute():
            result.update(failure_type="configuration_failed",
                          summary=f"Validation deployment path must be relative to Assets/: {relative_path}")
            return result
        deployed = unity_create_file(relative_path, content)
        if not deployed.get("success"):
            result.update(failure_type="deployment_failed",
                          summary=f"Deployment failed for {relative_path}: {deployed.get('error')}")
            return result
        readback = unity_read_file(relative_path)
        if not readback.get("success") or readback.get("content") != content:
            result.update(failure_type="deployment_failed",
                          summary=f"Deployment read-back failed for {relative_path}.")
            return result
        result["files_changed"].append(relative_path)

    compilation = validate_unity_compilation(timeout_seconds=timeout_seconds)
    result["compiler_errors"] = compilation.get("compiler_errors", [])
    result["stdout"] = _bounded_output(compilation.get("stdout", ""))
    result["stderr"] = _bounded_output(compilation.get("stderr", ""))
    result["unity_log"] = _bounded_output(compilation.get("unity_log", ""))
    result["exit_code"] = compilation.get("exit_code")
    if not compilation.get("success"):
        timed_out = any("timed out" in str(message).lower()
                        for message in compilation.get("unity_messages", []))
        has_csharp_error = any(isinstance(error, dict) and error.get("code", "").startswith("CS")
                               for error in result["compiler_errors"])
        config_error = any(token in " ".join(compilation.get("unity_messages", [])).lower()
                           for token in ("not configured", "does not exist", "invalid unity project"))
        failure_type = ("timeout" if timed_out else "compile_failed" if has_csharp_error
                        or result["compiler_errors"] else "configuration_failed" if config_error
                        else "unknown_error")
        result.update(failure_type=failure_type,
                      summary="Unity compilation timed out." if timed_out else
                      "Unity compilation failed." if has_csharp_error or result["compiler_errors"] else
                      "Unity validation configuration is incomplete." if config_error else
                      "Unity compilation exited unsuccessfully without a parsed C# diagnostic.")
        return result

    if playmode_assembly:
        runtime = run_unity_playmode_test(playmode_assembly, timeout_seconds=max(timeout_seconds, 900))
        result["runtime_errors"] = runtime.get("runtime_errors", [])
        result["test_failures"] = runtime.get("test_failures", [])
        result["stdout"] = _bounded_output("\n".join(filter(None, [result["stdout"], runtime.get("stdout", "")])))
        result["stderr"] = _bounded_output("\n".join(filter(None, [result["stderr"], runtime.get("stderr", "")])))
        result["unity_log"] = _bounded_output("\n".join(filter(None, [result["unity_log"], runtime.get("unity_log", "")])))
        result["exit_code"] = runtime.get("exit_code")
        if not runtime.get("success"):
            timed_out = any("timed out" in str(message).lower() for message in runtime.get("unity_messages", []))
            result.update(failure_type="timeout" if timed_out else "runtime_test_failed",
                          summary=f"PlayMode assembly {playmode_assembly} failed: "
                          + (runtime.get("error") or "; ".join(f.get("name", "test failed")
                                                               for f in result["test_failures"])
                             or "Unity Test Runner did not report a passing test."))
            return result

    result.update(success=True, failure_type="success",
                  summary="Unity compilation and requested runtime validation passed.")
    return result


T42_RUNTIME_CHECKS = (
    "Initial state -> Start",
    "Start -> Playing",
    "Playing -> Paused",
    "Paused -> Playing",
    "Player Death -> GameOver",
    "GameOver -> Restart",
    "Post-restart state",
    "Time scale reset",
    "Duplicate manager/HUD check",
    "Wave restart check",
    "Score/health/ammo reset",
)


def run_unity_t42_playmode_validation(timeout_seconds: int = 900) -> dict[str, Any]:
    """Run only the fixed T4.2 PlayMode test assembly in the configured Unity Editor."""
    validation = validate_unity_project()
    failure = {
        "success": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "unity_log": "",
        "test_results_xml": "",
        "runtime_checks": [],
        "compiler_errors": [],
        "unity_messages": [],
    }
    if not validation["success"]:
        failure["unity_messages"].append(validation["error"])
        return failure

    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        failure["unity_messages"].append("UNITY_EDITOR_PATH is not configured.")
        return failure
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        failure["unity_messages"].append(f"Configured Unity Editor executable does not exist: {editor}")
        return failure

    test_assembly = Path(validation["assets_path"]) / "Tests" / "PlayMode" / "T42RuntimeValidation.asmdef"
    test_source = Path(validation["assets_path"]) / "Tests" / "PlayMode" / "T42RuntimeValidationTests.cs"
    if not test_assembly.is_file() or not test_source.is_file():
        failure["unity_messages"].append(
            "T4.2 PlayMode test bridge is not deployed under Assets/Tests/PlayMode."
        )
        return failure

    with tempfile.TemporaryDirectory(
        prefix="unity-t42-playmode-", ignore_cleanup_errors=True
    ) as result_directory:
        results_path = Path(result_directory) / "t42-playmode-results.xml"
        log_path = Path(result_directory) / "t42-playmode.log"
        command = [
            str(editor),
            "-batchmode",
            "-runTests",
            "-projectPath", validation["project_path"],
            "-testPlatform", "PlayMode",
            "-assemblyNames", "T42RuntimeValidation",
            "-testResults", str(results_path),
            "-logFile", str(log_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            test_xml = results_path.read_text(encoding="utf-8", errors="replace") if results_path.is_file() else ""
            combined = "\n".join((stdout, stderr, unity_log))
            compiler_errors = parse_compiler_errors(combined)
            runtime_checks = []
            for line in combined.splitlines():
                match = re.search(r"\[T4\.2 RUNTIME\]\s*(.+?):\s*(PASS|FAIL)(?:\s*[-—:]\s*(.*))?$", line)
                if match:
                    runtime_checks.append({
                        "check": match.group(1).strip(),
                        "result": match.group(2).upper(),
                        "details": (match.group(3) or "").strip(),
                    })

            test_cases = []
            test_failures = []
            total_tests = 0
            if test_xml:
                try:
                    root = ET.fromstring(test_xml)
                    test_cases = [node for node in root.iter() if node.tag.split("}")[-1] == "test-case"]
                    total_tests = int(root.attrib.get("total", len(test_cases)))
                    for case in test_cases:
                        result_name = case.attrib.get("result", "Unknown")
                        if result_name.lower() not in ("passed", "skipped", "inconclusive"):
                            test_failures.append({
                                "name": case.attrib.get("fullname") or case.attrib.get("name", "unknown"),
                                "result": result_name,
                            })
                except (ET.ParseError, ValueError) as exc:
                    test_failures.append({"name": "test-results.xml", "result": f"Invalid XML: {exc}"})

            missing_checks = [
                check for check in T42_RUNTIME_CHECKS
                if not any(item["check"] == check and item["result"] == "PASS" for item in runtime_checks)
            ]
            unity_messages = parse_unity_messages(combined)
            if not test_xml:
                unity_messages.append("Unity did not produce a PlayMode test results file.")
            if completed.returncode != 0:
                unity_messages.append(f"Unity Test Runner exited with code {completed.returncode}.")
            return {
                "success": (completed.returncode == 0 and total_tests > 0
                            and not test_failures and not missing_checks and not compiler_errors),
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "unity_log": unity_log,
                "test_results_xml": test_xml,
                "test_total": total_tests,
                "test_failures": test_failures,
                "runtime_checks": runtime_checks,
                "missing_runtime_checks": missing_checks,
                "compiler_errors": compiler_errors,
                "unity_messages": unity_messages,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            unity_log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            return {
                **failure,
                "stdout": stdout,
                "stderr": stderr,
                "unity_log": unity_log,
                "compiler_errors": parse_compiler_errors("\n".join((stdout, stderr, unity_log))),
                "unity_messages": [f"Unity PlayMode validation timed out after {timeout_seconds} seconds."],
            }
        except OSError as exc:
            return {**failure, "stderr": str(exc),
                    "unity_messages": [f"Could not launch Unity PlayMode tests: {exc}"]}


T43_RUNTIME_CHECKS = (
    "Game starts successfully", "Wave 1 starts", "Wave 1 enemies spawn",
    "Wave 1 clears", "Wave 2 begins", "Wave 2 clears", "Wave 3 begins",
    "Wave 3 clears", "Controlled difficulty increase", "No duplicate wave execution",
    "No duplicate score awards", "No duplicate managers", "HUD reports current wave and score",
)


def run_unity_t43_playmode_validation(timeout_seconds: int = 900) -> dict[str, Any]:
    """Run the isolated T4.3 multi-wave PlayMode bridge and collect real measurements."""
    validation = validate_unity_project()
    report: dict[str, Any] = {
        "success": False, "exit_code": None, "runtime_checks": [],
        "compiler_errors": [], "unity_messages": [], "test_results_xml": "",
    }
    if not validation["success"]:
        report["unity_messages"].append(validation["error"])
        return report
    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        report["unity_messages"].append("UNITY_EDITOR_PATH is not configured.")
        return report
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        report["unity_messages"].append(f"Configured Unity Editor executable does not exist: {editor}")
        return report

    assets = Path(validation["assets_path"])
    asmdef = assets / "Tests" / "PlayMode" / "T43" / "T43MultiWaveValidation.asmdef"
    source = assets / "Tests" / "PlayMode" / "T43" / "T43MultiWaveValidationTests.cs"
    if not asmdef.is_file() or not source.is_file():
        report["unity_messages"].append("T4.3 PlayMode test bridge is not deployed under Assets/Tests/PlayMode.")
        return report

    with tempfile.TemporaryDirectory(prefix="unity-t43-playmode-", ignore_cleanup_errors=True) as result_directory:
        results_path = Path(result_directory) / "t43-playmode-results.xml"
        log_path = Path(result_directory) / "t43-playmode.log"
        command = [str(editor), "-batchmode", "-runTests", "-projectPath", validation["project_path"],
                   "-testPlatform", "PlayMode", "-assemblyNames", "T43MultiWaveValidation",
                   "-testResults", str(results_path), "-logFile", str(log_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
            output = "\n".join((completed.stdout or "", completed.stderr or "",
                                 log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""))
            report["stdout"] = completed.stdout or ""
            report["stderr"] = completed.stderr or ""
            report["unity_log"] = output
            report["test_results_xml"] = (results_path.read_text(encoding="utf-8", errors="replace")
                                           if results_path.is_file() else "")
            report["exit_code"] = completed.returncode
            report["compiler_errors"] = parse_compiler_errors(output)
            for line in output.splitlines():
                match = re.search(r"\[T4\.3 RUNTIME\]\s*(.+?):\s*(PASS|FAIL)(?:\s*[-–—]\s*(.*))?$", line)
                if match:
                    report["runtime_checks"].append({"check": match.group(1).strip(),
                        "result": match.group(2).upper(), "details": (match.group(3) or "").strip()})
            xml = report["test_results_xml"]
            failures, total = [], 0
            if xml:
                try:
                    root = ET.fromstring(xml)
                    cases = [node for node in root.iter() if node.tag.split("}")[-1] == "test-case"]
                    total = int(root.attrib.get("total", len(cases)))
                    failures = []
                    for node in cases:
                        if node.attrib.get("result", "").lower() == "passed":
                            continue
                        failure_node = next((child for child in node if child.tag.split("}")[-1] == "failure"), None)
                        failures.append({
                            "name": node.attrib.get("fullname", node.attrib.get("name", "unknown")),
                            "result": node.attrib.get("result", "Unknown"),
                            "message": failure_node.findtext("message", default="") if failure_node is not None else "",
                            "stack_trace": failure_node.findtext("stack-trace", default="") if failure_node is not None else "",
                        })
                except (ET.ParseError, ValueError) as exc:
                    failures = [{"name": "test-results.xml", "result": f"Invalid XML: {exc}"}]
            report["test_total"] = total
            report["test_failures"] = failures
            report["missing_runtime_checks"] = [name for name in T43_RUNTIME_CHECKS
                if not any(c["check"] == name and c["result"] == "PASS" for c in report["runtime_checks"])]
            report["unity_messages"] = parse_unity_messages(output)
            if not xml:
                report["unity_messages"].append("Unity did not produce a PlayMode results file.")
            if completed.returncode != 0:
                report["unity_messages"].append(f"Unity Test Runner exited with code {completed.returncode}.")
            report["success"] = (completed.returncode == 0 and total > 0 and not failures
                and not report["missing_runtime_checks"] and not report["compiler_errors"])
            return report
        except subprocess.TimeoutExpired as exc:
            report["stdout"] = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            report["stderr"] = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            report["unity_messages"].append(f"Unity PlayMode validation timed out after {timeout_seconds} seconds.")
            return report
        except OSError as exc:
            report["stderr"] = str(exc)
            report["unity_messages"].append(f"Could not launch Unity PlayMode tests: {exc}")
            return report


def run_unity_editor_method(method_name: str, success_marker: str,
                            timeout_seconds: int = 900) -> dict[str, Any]:
    """Run one fixed Editor automation method and preserve the full Unity log."""
    validation = validate_unity_project()
    report: dict[str, Any] = {"success": False, "exit_code": None,
                              "compiler_errors": [], "unity_messages": []}
    if not validation["success"]:
        report["unity_messages"].append(validation["error"])
        return report
    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        report["unity_messages"].append("UNITY_EDITOR_PATH is not configured.")
        return report
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        report["unity_messages"].append(f"Configured Unity Editor executable does not exist: {editor}")
        return report

    with tempfile.TemporaryDirectory(prefix="unity-scene-prep-", ignore_cleanup_errors=True) as temp_dir:
        log_path = Path(temp_dir) / "scene-preparation.log"
        command = [str(editor), "-batchmode", "-quit", "-projectPath", validation["project_path"],
                   "-executeMethod", method_name, "-logFile", str(log_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
            output = "\n".join((completed.stdout or "", completed.stderr or "",
                log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""))
            report.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                "stderr": completed.stderr or "", "unity_log": output,
                "compiler_errors": parse_compiler_errors(output),
                "unity_messages": parse_unity_messages(output)})
            report["success"] = (completed.returncode == 0 and not report["compiler_errors"]
                                 and success_marker in output)
            if completed.returncode != 0:
                report["unity_messages"].append(f"Unity Editor automation exited with code {completed.returncode}.")
            if success_marker not in output:
                report["unity_messages"].append(f"Editor method did not report success marker: {success_marker}")
            return report
        except subprocess.TimeoutExpired as exc:
            report["stdout"] = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            report["stderr"] = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            report["unity_messages"].append(f"Unity Editor automation timed out after {timeout_seconds} seconds.")
            return report
        except OSError as exc:
            report["stderr"] = str(exc)
            report["unity_messages"].append(f"Could not launch Unity Editor automation: {exc}")
            return report


SCENE_PREPARATION_CHECKS = (
    "scene loads", "exactly one player", "exactly one WaveManager",
    "exactly one GameStateManager", "exactly one ScoreManager", "HUD initializes once",
    "PlayerHealth initializes", "PlayerWeapon initializes", "Main Camera initializes",
    "player and camera references", "GameStateManager binds player and waves",
    "WaveManager references prefab and spawn points", "enemy prefab has health, controller, and collider",
    "WaveManager starts",
    "at least one enemy spawns", "spawned EnemyController finds Player by tag",
    "no NullReferenceException during initialization", "no duplicate manager or HUD instances after spawn",
)


def run_unity_scene_preparation_smoke(timeout_seconds: int = 900) -> dict[str, Any]:
    """Run only the isolated minimal-scene PlayMode smoke test."""
    validation = validate_unity_project()
    report: dict[str, Any] = {"success": False, "exit_code": None,
        "runtime_checks": [], "compiler_errors": [], "unity_messages": [], "test_results_xml": ""}
    if not validation["success"]:
        report["unity_messages"].append(validation["error"])
        return report
    editor_setting = os.getenv("UNITY_EDITOR_PATH")
    if not editor_setting:
        report["unity_messages"].append("UNITY_EDITOR_PATH is not configured.")
        return report
    editor = Path(editor_setting).expanduser().resolve()
    if not editor.is_file():
        report["unity_messages"].append(f"Configured Unity Editor executable does not exist: {editor}")
        return report
    assets = Path(validation["assets_path"])
    asmdef = assets / "Tests" / "PlayMode" / "ScenePreparation" / "ScenePreparationSmoke.asmdef"
    source = assets / "Tests" / "PlayMode" / "ScenePreparation" / "ScenePreparationSmokeTests.cs"
    if not asmdef.is_file() or not source.is_file():
        report["unity_messages"].append("Scene preparation smoke-test assembly is not deployed.")
        return report

    with tempfile.TemporaryDirectory(prefix="unity-prep-playmode-", ignore_cleanup_errors=True) as temp_dir:
        results_path = Path(temp_dir) / "scene-preparation-results.xml"
        log_path = Path(temp_dir) / "scene-preparation-playmode.log"
        command = [str(editor), "-batchmode", "-runTests", "-projectPath", validation["project_path"],
                   "-testPlatform", "PlayMode", "-assemblyNames", "ScenePreparationSmoke",
                   "-testResults", str(results_path), "-logFile", str(log_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
            output = "\n".join((completed.stdout or "", completed.stderr or "",
                log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""))
            report.update({"exit_code": completed.returncode, "stdout": completed.stdout or "",
                "stderr": completed.stderr or "", "unity_log": output,
                "test_results_xml": results_path.read_text(encoding="utf-8", errors="replace")
                    if results_path.is_file() else "",
                "compiler_errors": parse_compiler_errors(output),
                "unity_messages": parse_unity_messages(output)})
            for line in output.splitlines():
                match = re.search(r"\[SCENE PREP SMOKE\]\s*(.+?):\s*(PASS|FAIL)(?:\s*[-–—]\s*(.*))?$", line)
                if match:
                    report["runtime_checks"].append({"check": match.group(1).strip(),
                        "result": match.group(2).upper(), "details": (match.group(3) or "").strip()})
            total, failures = 0, []
            if report["test_results_xml"]:
                try:
                    root = ET.fromstring(report["test_results_xml"])
                    cases = [node for node in root.iter() if node.tag.split("}")[-1] == "test-case"]
                    total = int(root.attrib.get("total", len(cases)))
                    failures = [{"name": node.attrib.get("fullname", node.attrib.get("name", "unknown")),
                                 "result": node.attrib.get("result", "Unknown")}
                                for node in cases if node.attrib.get("result", "").lower() != "passed"]
                except (ET.ParseError, ValueError) as exc:
                    failures = [{"name": "test-results.xml", "result": f"Invalid XML: {exc}"}]
            report["test_total"] = total
            report["test_failures"] = failures
            report["missing_runtime_checks"] = [name for name in SCENE_PREPARATION_CHECKS
                if not any(item["check"] == name and item["result"] == "PASS"
                           for item in report["runtime_checks"])]
            if not report["test_results_xml"]:
                report["unity_messages"].append("Unity did not produce a smoke-test results file.")
            report["success"] = (completed.returncode == 0 and total > 0 and not failures
                and not report["missing_runtime_checks"] and not report["compiler_errors"])
            return report
        except subprocess.TimeoutExpired as exc:
            report["stdout"] = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            report["stderr"] = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            report["unity_messages"].append(f"Unity smoke test timed out after {timeout_seconds} seconds.")
            return report
        except OSError as exc:
            report["stderr"] = str(exc)
            report["unity_messages"].append(f"Could not launch Unity smoke test: {exc}")
            return report


def run_player_health_integration_test(player_health_source: str) -> dict[str, Any]:
    """Deploy and validate only the existing PlayerHealth script."""
    report: dict[str, Any] = {"success": False}
    validation = validate_unity_project()
    report["project_validation"] = validation
    if not validation["success"]:
        print(f"Unity project validation failed: {validation['error']}")
        return report

    assets_exists = Path(validation["assets_path"]).is_dir()
    report["assets_exists"] = assets_exists
    if not assets_exists:
        print("Unity project's Assets directory is missing.")
        return report

    relative_path = "Assets/Scripts/PlayerHealth.cs"
    existence = unity_file_exists(relative_path)
    report["file_exists"] = existence
    if not existence["success"]:
        print(f"Unity file check failed: {existence.get('error') or 'unknown error'}")
        return report

    readback = unity_read_file(relative_path)
    report["readback"] = {
        "success": readback["success"] and readback.get("content") == player_health_source,
        "path": relative_path,
        "error": readback.get("error"),
    }
    if not report["readback"]["success"]:
        print("PlayerHealth does not match the existing T2.2 source; refusing to modify the Unity project.")
        return report

    # This integration check is read-only when the project already has the exact source.
    # Avoid rewriting an identical file so compilation validation does not modify the project.
    deployment = {
        "success": True,
        "path": relative_path,
        "bytes_written": 0,
        "already_matches": True,
        "error": None,
    }
    report["deployment"] = deployment

    compilation = validate_unity_compilation()
    report["compilation"] = compilation
    report["success"] = compilation["success"]
    print("Unity PlayerHealth integration report:")
    print(f"  Project: {validation['project_path']}")
    print(f"  Assets exists: {assets_exists}")
    print("  Deployment: existing file matches source; no write performed")
    print("  File exists/read-back: SUCCESS")
    print(f"  Compilation: {'SUCCESS' if compilation['success'] else 'FAIL'} (exit code: {compilation['exit_code']})")
    print(f"  Compiler errors: {len(compilation['compiler_errors'])}")
    print(f"  Unity log captured: {bool(compilation.get('unity_log'))}")
    for diagnostic in compilation["compiler_errors"]:
        print(f"    - {diagnostic}")
    return report


CURRENT_GAMEPLAY_SCRIPTS = (
    "PlayerController.cs",
    "ThirdPersonCamera.cs",
    "PlayerHealth.cs",
    "PlayerWeapon.cs",
    "EnemyHealth.cs",
    "EnemyController.cs",
    "WaveManager.cs",
)


def run_gameplay_checkpoint(workspace_read_file) -> dict[str, Any]:
    """Deploy and compile the fixed current script set, without touching scenes."""
    report: dict[str, Any] = {
        "success": False,
        "files_deployed": [],
        "files_verified": [],
    }
    validation = validate_unity_project()
    report["project_validation"] = validation
    if not validation["success"]:
        report["error"] = validation["error"]
        print(f"Unity project validation failed: {validation['error']}")
        return report

    if not Path(validation["assets_path"]).is_dir():
        report["error"] = "Unity project's Assets directory is missing."
        print(report["error"])
        return report

    # Read and validate all workspace sources before writing any of them.
    sources = {}
    workspace_base = "UnityProject/Assets/Scripts"
    for filename in CURRENT_GAMEPLAY_SCRIPTS:
        workspace_path = f"{workspace_base}/{filename}"
        source_result = workspace_read_file(workspace_path)
        if not source_result.get("success"):
            report["error"] = (
                f"Could not read workspace script {workspace_path}: "
                f"{source_result.get('error', 'read failed')}"
            )
            print(report["error"])
            return report
        sources[filename] = source_result["content"]

    for filename in CURRENT_GAMEPLAY_SCRIPTS:
        relative_path = f"Assets/Scripts/{filename}"
        deployment = deploy_script_to_unity(relative_path, sources[filename])
        if not deployment.get("success"):
            report["error"] = f"Deployment failed for {relative_path}: {deployment.get('error')}"
            report["deployment_failure"] = deployment
            print(report["error"])
            return report
        report["files_deployed"].append(relative_path)

        existence = unity_file_exists(relative_path)
        readback = unity_read_file(relative_path) if existence.get("success") and existence.get("exists") else None
        if (
            not existence.get("success")
            or not existence.get("exists")
            or not readback
            or not readback.get("success")
            or readback.get("content") != sources[filename]
        ):
            report["error"] = f"Deployment verification failed for {relative_path}"
            report["verification_failure"] = {
                "file_exists": existence,
                "readback_success": bool(readback and readback.get("success")),
            }
            print(report["error"])
            return report
        report["files_verified"].append(relative_path)

    compilation = validate_unity_compilation()
    report["compilation"] = compilation
    report["unity_exit_code"] = compilation["exit_code"]
    report["compiler_error_count"] = len(compilation["compiler_errors"])
    report["success"] = compilation["success"]

    print("Unity gameplay integration checkpoint:")
    print(f"  Project: {validation['project_path']}")
    print(f"  Files deployed: {len(report['files_deployed'])}")
    for path in report["files_deployed"]:
        print(f"    - {path}")
    print(f"  Files verified: {len(report['files_verified'])}/{len(CURRENT_GAMEPLAY_SCRIPTS)}")
    print(f"  Unity exit code: {compilation['exit_code']}")
    print(f"  C# compiler errors: {report['compiler_error_count']}")
    print(f"  Compilation: {'SUCCESS' if compilation['success'] else 'FAIL'}")
    print(f"  Unity log captured: {bool(compilation.get('unity_log'))}")
    print(f"  Generic Unity messages: {len(compilation.get('unity_messages', []))}")
    for diagnostic in compilation["compiler_errors"]:
        print(f"    Compiler error: {diagnostic}")
    for message in compilation.get("unity_messages", []):
        print(f"    Unity message: {message}")
    return report


UNITY_TOOLS = {
    "validate_unity_project": validate_unity_project,
    "unity_read_file": unity_read_file,
    "unity_file_exists": unity_file_exists,
    "deploy_script_to_unity": deploy_script_to_unity,
    "unity_delete_file": unity_delete_file,
    "unity_enable_legacy_input_compatibility": unity_enable_legacy_input_compatibility,
    "validate_unity_compilation": validate_unity_compilation,
    "run_unity_playmode_test": run_unity_playmode_test,
    "validate_unity_files": validate_unity_files,
    "create_unity_scene": create_unity_scene,
}
