from google import genai
from google.genai import types
import os
import time
import json
import argparse
import re
from pathlib import Path
from google.genai import errors
from typing import Any, Literal, TypedDict
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

try:
    from .capabilities import build_capability_plan
    from .game_construction import CAPABILITY_TASK_EXECUTORS
    from .unity_integration import (
        UNITY_TOOLS,
        run_gameplay_checkpoint,
        run_player_health_integration_test,
        run_unity_editor_method,
        run_unity_scene_preparation_smoke,
        run_unity_t43_playmode_validation,
        run_unity_t42_playmode_validation,
        unity_create_file,
        unity_delete_file,
    )
except ImportError:
    from capabilities import build_capability_plan
    from game_construction import CAPABILITY_TASK_EXECUTORS
    from unity_integration import (
        UNITY_TOOLS,
        run_gameplay_checkpoint,
        run_player_health_integration_test,
        run_unity_editor_method,
        run_unity_scene_preparation_smoke,
        run_unity_t43_playmode_validation,
        run_unity_t42_playmode_validation,
        unity_create_file,
        unity_delete_file,
    )

MODEL = "gemini-3.8-flash"
OFFLINE_MODE = True
SAMPLE_PLAN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample_plan.json")
WORKSPACE_ROOT = Path(os.path.dirname(os.path.dirname(__file__))) / "agent_workspace"
MAX_ATTEMPTS = 3
try:
    MAX_REPAIR_ATTEMPTS = max(0, int(os.getenv("MAX_REPAIR_ATTEMPTS", "2")))
except ValueError:
    MAX_REPAIR_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2
TEMPORARY_ERROR_CODES = (408, 429, 500, 502, 503, 504)


class Task(BaseModel):
    id: str
    description: str
    dependencies: list[str]
    capability_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)


class Phase(BaseModel):
    id: int
    name: str
    tasks: list[Task]


class Dependency(BaseModel):
    task: str
    depends_on: list[str]


class ProjectPlan(BaseModel):
    project_name: str
    requirements: list[str]
    phases: list[Phase]
    dependencies: list[Dependency]
    assumptions: list[str]
    genre: str = ""
    camera_perspective: str = ""
    core_loop: str = ""
    controls: list[str] = Field(default_factory=list)
    game_objects: list[str] = Field(default_factory=list)
    selected_capabilities: list[str] = Field(default_factory=list)
    scene_requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    task_id: str
    success: bool
    failure_type: Literal[
        "success", "compile_failed", "runtime_test_failed", "timeout",
        "deployment_failed", "configuration_failed", "unknown_error",
    ]
    summary: str
    compiler_errors: list[dict[str, Any]] = Field(default_factory=list)
    runtime_errors: list[str] = Field(default_factory=list)
    test_failures: list[dict[str, Any]] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    unity_log: str = ""
    exit_code: int | None = None
    files_changed: list[str] = Field(default_factory=list)
    attempt_number: int = 1


class FailureDiagnosis(BaseModel):
    failure_type: Literal[
        "compile_failed", "runtime_test_failed", "timeout", "deployment_failed",
        "configuration_failed", "unknown_error",
    ]
    root_cause: str
    relevant_files: list[str]
    repair_required: bool
    automatically_fixable: bool
    repair_strategy: str
    evidence_quotes: list[str]
    uncertainty: str = ""
    diagnosis_source: Literal["gemini", "deterministic_fallback"] = "gemini"
    llm_error: str | None = None
    error_code: str | None = None
    compiler_message: str | None = None
    failing_test: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    stack_trace: str | None = None


class RepairProposal(BaseModel):
    file_path: str
    original_text: str
    replacement_text: str
    reason: str
    evidence_quotes: list[str]


def load_plan_from_json(path: str) -> ProjectPlan:
    """Load a JSON plan from disk and validate it with the existing schema."""
    with open(path, "r", encoding="utf-8") as plan_file:
        return ProjectPlan.model_validate_json(plan_file.read())


# AgentState is the shared data passed from one LangGraph node to the next.
class AgentState(TypedDict):
    plan: ProjectPlan
    completed_tasks: list[str]
    available_tasks: list[str]
    current_task: str | None
    execution_results: dict[str, str]
    tool_results: list[dict[str, Any]]
    failed_tasks: list[str]
    execution_error: str | None
    task_execution: dict[str, Any] | None
    validation_request: dict[str, Any] | None
    validation_result: dict[str, Any] | None
    failure_diagnosis: dict[str, Any] | None
    diagnosis_attempt: dict[str, Any] | None
    repair_attempts: int
    max_repair_attempts: int
    validation_history: list[dict[str, Any]]
    repair_action: dict[str, Any] | None
    task_runs: list[dict[str, Any]]


def get_all_tasks(plan):
    """Flatten phase tasks so dependencies can be looked up by task ID."""
    # Tasks are grouped by phase for display, but a flat lookup makes it easy
    # to check whether a dependency ID refers to a real task in the plan.
    tasks_by_id = {}
    for phase in plan.phases:
        for task in phase.tasks:
            if task.id in tasks_by_id:
                print(f"Warning: duplicate task ID '{task.id}' found in the plan.")
                continue
            tasks_by_id[task.id] = task
    return tasks_by_id


def get_available_tasks(plan, completed_tasks):
    """Return tasks whose dependencies have all been completed."""
    all_tasks = get_all_tasks(plan)
    completed = set(completed_tasks)
    available_tasks = []
    warned_missing_dependencies = set()

    for task in all_tasks.values():
        missing_dependencies = [
            dependency_id
            for dependency_id in task.dependencies
            if dependency_id not in all_tasks
        ]
        for dependency_id in missing_dependencies:
            warning = (task.id, dependency_id)
            if warning not in warned_missing_dependencies:
                print(
                    f"Warning: task '{task.id}' depends on missing task ID "
                    f"'{dependency_id}'."
                )
                warned_missing_dependencies.add(warning)

        # completed_tasks records the task IDs already finished. A task can
        # run only when every dependency is a real task and is in that list.
        if (
            task.id not in completed
            and all(
                dependency_id in all_tasks and dependency_id in completed
                for dependency_id in task.dependencies
            )
        ):
            available_tasks.append(task)

    return available_tasks


def print_available_tasks(plan, completed_tasks):
    """Print the current deterministic task queue."""
    all_tasks = get_all_tasks(plan)
    available_tasks = get_available_tasks(plan, completed_tasks)

    print("\n========== TASK QUEUE ==========")
    if available_tasks:
        for task in available_tasks:
            dependencies = ", ".join(task.dependencies) or "None"
            print(f"{task.id} - {task.description}")
            print(f"Dependencies: {dependencies}")
    elif set(all_tasks).issubset(set(completed_tasks)):
        print("All tasks are completed.")
    else:
        print(
            "No tasks are available, but some tasks remain incomplete. "
            "The dependency graph may contain a circular or unresolved dependency."
        )


def mark_task_completed(completed_tasks, task_id):
    """Record completion once; repeating the same ID is safe."""
    if task_id not in completed_tasks:
        completed_tasks.append(task_id)


def _workspace_path(path: str) -> tuple[Path | None, str | None]:
    """Resolve a tool path beneath the dedicated workspace and reject escapes."""
    workspace = WORKSPACE_ROOT.resolve()
    requested = Path(path)
    candidate = (workspace / requested).resolve() if not requested.is_absolute() else requested.resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return None, "Path is outside the allowed workspace"
    return candidate, None


def _tool_result(tool: str, path: str, success: bool, **details) -> dict[str, Any]:
    result = {"success": success, "tool": tool, "path": path}
    result.update(details)
    return result


def create_directory(path: str) -> dict[str, Any]:
    """Create a directory only inside agent_workspace."""
    target, error = _workspace_path(path)
    if error:
        return _tool_result("create_directory", path, False, error=error)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return _tool_result("create_directory", path, True)
    except OSError as exc:
        return _tool_result("create_directory", path, False, error=str(exc))


def create_file(path: str, content: str) -> dict[str, Any]:
    """Create or update a text file inside agent_workspace."""
    target, error = _workspace_path(path)
    if error:
        return _tool_result("create_file", path, False, error=error)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _tool_result("create_file", path, True)
    except OSError as exc:
        return _tool_result("create_file", path, False, error=str(exc))


def read_file(path: str) -> dict[str, Any]:
    """Read a UTF-8 text file only from agent_workspace."""
    target, error = _workspace_path(path)
    if error:
        return _tool_result("read_file", path, False, error=error)
    try:
        return _tool_result("read_file", path, True, content=target.read_text(encoding="utf-8"))
    except OSError as exc:
        return _tool_result("read_file", path, False, error=str(exc))


def file_exists(path: str) -> dict[str, Any]:
    """Check for a file inside agent_workspace."""
    target, error = _workspace_path(path)
    if error:
        return _tool_result("file_exists", path, False, error=error)
    return _tool_result("file_exists", path, True, exists=target.is_file())


def delete_file(path: str) -> dict[str, Any]:
    """Delete one explicit file inside agent_workspace."""
    target, error = _workspace_path(path)
    if error:
        return _tool_result("delete_file", path, False, error=error)
    try:
        if target.is_file():
            target.unlink()
        return _tool_result("delete_file", path, True)
    except OSError as exc:
        return _tool_result("delete_file", path, False, error=str(exc))


# Tools are small, explicit capabilities; the LLM cannot call arbitrary code.
TOOLS = {
    "create_directory": create_directory,
    "create_file": create_file,
    "read_file": read_file,
    "file_exists": file_exists,
    "delete_file": delete_file,
}

# Keep Unity capabilities explicit in their names while exposing them through
# the agent's shared registry alongside the existing workspace tools.
TOOLS.update(UNITY_TOOLS)


def _run_tool(tool_name: str, *args) -> dict[str, Any]:
    """Run one registered tool and print its auditable result."""
    result = TOOLS[tool_name](*args)
    print("\n========== TOOL EXECUTION ==========")
    print(f"\nTool: {tool_name}")
    print(f"Path: {result.get('path', '-')}")
    print(f"Result: {'SUCCESS' if result['success'] else 'FAILURE'}")
    if result.get("error"):
        print(f"Reason: {result['error']}")
    return result


def execute_t1_1(state: AgentState) -> dict[str, Any]:
    paths = [
        "UnityProject/Assets/Scenes",
        "UnityProject/Assets/Scripts",
        "UnityProject/Assets/Prefabs",
        "UnityProject/Assets/Materials",
        "UnityProject/Assets/UI",
    ]
    results = [_run_tool("create_directory", path) for path in paths]
    return {"success": all(result["success"] for result in results), "tool_results": results}


def execute_t1_2(state: AgentState) -> dict[str, Any]:
    path = "UnityProject/Assets/Scenes/MainSceneSpec.json"
    content = json.dumps({
        "scene": "MainScene",
        "objects": ["Ground", "Building_01", "Building_02", "PlayerSpawn"],
    }, indent=2)
    result = _run_tool("create_file", path, content)
    return {"success": result["success"], "tool_results": [result]}


def execute_t1_3(state: AgentState) -> dict[str, Any]:
    path = "UnityProject/Assets/Scenes/EntitiesSpec.json"
    content = json.dumps({
        "entities": ["Player", "Enemy"],
        "layers": ["Default", "Player", "Enemy", "Environment"],
        "physics": {"gravity": [0, -9.81, 0], "collision_detection": True},
    }, indent=2)
    result = _run_tool("create_file", path, content)
    return {"success": result["success"], "tool_results": [result]}


PLAYER_CONTROLLER_SOURCE = '''using UnityEngine;

// Basic third-person movement using Unity's CharacterController.
[RequireComponent(typeof(CharacterController))]
public class PlayerController : MonoBehaviour
{
    [SerializeField] private Transform cameraTransform;
    [SerializeField] private float movementSpeed = 5f;
    [SerializeField] private float rotationSpeed = 12f;
    [SerializeField] private float gravity = -9.81f;

    private CharacterController characterController;
    private float verticalVelocity;

    private void Awake()
    {
        characterController = GetComponent<CharacterController>();
        if (cameraTransform == null && Camera.main != null)
            cameraTransform = Camera.main.transform;
    }

    private void Update()
    {
        // Read WASD (or the configured Unity horizontal/vertical axes).
        Vector2 input = new Vector2(Input.GetAxisRaw("Horizontal"), Input.GetAxisRaw("Vertical"));
        input = Vector2.ClampMagnitude(input, 1f);

        Vector3 forward = cameraTransform != null ? cameraTransform.forward : transform.forward;
        Vector3 right = cameraTransform != null ? cameraTransform.right : transform.right;
        forward.y = 0f;
        right.y = 0f;
        forward.Normalize();
        right.Normalize();

        Vector3 moveDirection = (forward * input.y + right * input.x).normalized;
        characterController.Move(moveDirection * movementSpeed * Time.deltaTime);

        // Turn toward the direction of travel, leaving the player still when idle.
        if (moveDirection.sqrMagnitude > 0.001f)
        {
            Quaternion targetRotation = Quaternion.LookRotation(moveDirection);
            transform.rotation = Quaternion.Slerp(transform.rotation, targetRotation,
                rotationSpeed * Time.deltaTime);
        }

        if (characterController.isGrounded && verticalVelocity < 0f)
            verticalVelocity = -2f;
        verticalVelocity += gravity * Time.deltaTime;
        characterController.Move(Vector3.up * verticalVelocity * Time.deltaTime);
    }
}
'''


THIRD_PERSON_CAMERA_SOURCE = '''using UnityEngine;

// Smooth orbit camera that follows and looks toward a target Transform.
public class ThirdPersonCamera : MonoBehaviour
{
    [SerializeField] private Transform target;
    [SerializeField] private float distance = 5f;
    [SerializeField] private float height = 2f;
    [SerializeField] private float mouseSensitivity = 3f;
    [SerializeField] private float minimumPitch = -25f;
    [SerializeField] private float maximumPitch = 65f;
    [SerializeField] private float smoothSpeed = 12f;

    private float yaw;
    private float pitch = 15f;

    private void LateUpdate()
    {
        if (target == null)
            return;

        // Mouse movement changes the orbit angles; pitch is clamped to avoid flipping.
        yaw += Input.GetAxis("Mouse X") * mouseSensitivity;
        pitch -= Input.GetAxis("Mouse Y") * mouseSensitivity;
        pitch = Mathf.Clamp(pitch, minimumPitch, maximumPitch);

        Vector3 lookPoint = target.position + Vector3.up * height;
        Quaternion orbit = Quaternion.Euler(pitch, yaw, 0f);
        Vector3 desiredPosition = lookPoint - orbit * Vector3.forward * distance;

        // Smooth both the camera position and its look-at rotation.
        float blend = 1f - Mathf.Exp(-smoothSpeed * Time.deltaTime);
        transform.position = Vector3.Lerp(transform.position, desiredPosition, blend);
        Quaternion desiredRotation = Quaternion.LookRotation(lookPoint - transform.position);
        transform.rotation = Quaternion.Slerp(transform.rotation, desiredRotation, blend);
    }
}
'''


def execute_t2_1(state: AgentState) -> dict[str, Any]:
    """Create and verify the two focused T2.1 source files through safe tools."""
    file_sources = [
        ("UnityProject/Assets/Scripts/PlayerController.cs", PLAYER_CONTROLLER_SOURCE),
        ("UnityProject/Assets/Scripts/ThirdPersonCamera.cs", THIRD_PERSON_CAMERA_SOURCE),
    ]
    tool_results = []
    created_files = []
    for path, source in file_sources:
        created = _run_tool("create_file", path, source)
        tool_results.append(created)
        if not created["success"]:
            return {
                "success": False,
                "error": f"Could not create {path}: {created.get('error', 'unknown tool error')}",
                "tool_results": tool_results,
                "files_created": created_files,
                "verified": False,
            }

        verified = _run_tool("file_exists", path)
        tool_results.append(verified)
        if not verified["success"] or not verified.get("exists"):
            return {
                "success": False,
                "error": f"File verification failed for {path}",
                "tool_results": tool_results,
                "files_created": created_files,
                "verified": False,
            }
        created_files.append(path)

    return {
        "success": True,
        "task_id": "T2.1",
        "files_created": created_files,
        "verified": True,
        "tool_results": tool_results,
    }


PLAYER_HEALTH_SOURCE = '''using UnityEngine;
using UnityEngine.Events;

// Owns the player's health and one-time death notification only.
public class PlayerHealth : MonoBehaviour
{
    [SerializeField, Min(1f)] private float maxHealth = 100f;
    [SerializeField] private UnityEvent onDeath;

    public float CurrentHealth { get; private set; }
    public float MaxHealth => maxHealth;
    public bool IsDead { get; private set; }

    private void Awake()
    {
        // Each player starts with the configured maximum health.
        CurrentHealth = maxHealth;
        IsDead = false;
    }

    public void TakeDamage(float amount)
    {
        if (IsDead || amount <= 0f)
            return;

        CurrentHealth = Mathf.Max(0f, CurrentHealth - amount);
        if (CurrentHealth <= 0f)
            Die();
    }

    private void Die()
    {
        // Guard against duplicate death handling if damage is applied again.
        if (IsDead)
            return;

        IsDead = true;
        onDeath?.Invoke();
    }
}
'''


def execute_t2_2(state: AgentState) -> dict[str, Any]:
    """Create PlayerHealth.cs and verify it using the safe workspace tools."""
    path = "UnityProject/Assets/Scripts/PlayerHealth.cs"
    created = _run_tool("create_file", path, PLAYER_HEALTH_SOURCE)
    tool_results = [created]
    if not created["success"]:
        return {
            "success": False,
            "task_id": "T2.2",
            "error": created.get("error", "File creation failed"),
            "tool_results": tool_results,
            "verified": False,
        }

    verified = _run_tool("file_exists", path)
    tool_results.append(verified)
    if not verified["success"] or not verified.get("exists"):
        return {
            "success": False,
            "task_id": "T2.2",
            "error": f"File verification failed for {path}",
            "tool_results": tool_results,
            "verified": False,
        }

    return {
        "success": True,
        "task_id": "T2.2",
        "files_created": [path],
        "verified": True,
        "tool_results": tool_results,
    }


PLAYER_WEAPON_SOURCE = '''using System.Collections;
using UnityEngine;

// Generic damage contract for future targets; this file does not depend on an Enemy class.
public interface IDamageable
{
    void TakeDamage(float amount);
}

// Simple hitscan weapon with ammunition, a fire-rate limit, and timed reloading.
public class PlayerWeapon : MonoBehaviour
{
    [SerializeField] private Camera aimCamera;
    [SerializeField, Min(0f)] private float damage = 25f;
    [SerializeField, Min(0f)] private float range = 100f;
    [SerializeField, Min(0.01f)] private float fireRate = 5f;
    [SerializeField, Min(1)] private int magazineSize = 12;
    [SerializeField, Min(0f)] private float reloadTime = 1.5f;

    private int currentAmmo;
    private float nextFireTime;
    private bool isReloading;

    public int CurrentAmmo => currentAmmo;
    public int MagazineSize => magazineSize;
    public bool IsReloading => isReloading;

    private void Awake()
    {
        currentAmmo = magazineSize;
        if (aimCamera == null)
            aimCamera = Camera.main;
    }

    public void Fire()
    {
        if (isReloading || currentAmmo <= 0 || Time.time < nextFireTime)
            return;

        if (aimCamera == null)
            aimCamera = Camera.main;
        if (aimCamera == null)
            return;

        // A shot is consumed when fired, whether or not it hits a damageable target.
        currentAmmo--;
        nextFireTime = Time.time + 1f / Mathf.Max(fireRate, 0.01f);

        if (Physics.Raycast(aimCamera.transform.position, aimCamera.transform.forward,
            out RaycastHit hit, range))
        {
            // Targets can opt in by implementing IDamageable; no enemy type is required.
            if (hit.collider.TryGetComponent<IDamageable>(out IDamageable damageable))
                damageable.TakeDamage(damage);
        }
    }

    public void Reload()
    {
        if (!isReloading && currentAmmo < magazineSize)
            StartCoroutine(ReloadRoutine());
    }

    private IEnumerator ReloadRoutine()
    {
        isReloading = true;
        yield return new WaitForSeconds(reloadTime);
        currentAmmo = magazineSize;
        isReloading = false;
    }
}
'''


def execute_t2_3(state: AgentState) -> dict[str, Any]:
    """Create and verify the standalone player weapon component."""
    path = "UnityProject/Assets/Scripts/PlayerWeapon.cs"
    created = _run_tool("create_file", path, PLAYER_WEAPON_SOURCE)
    tool_results = [created]
    if not created["success"]:
        return {
            "success": False,
            "task_id": "T2.3",
            "error": created.get("error", "File creation failed"),
            "tool_results": tool_results,
            "verified": False,
        }

    verified = _run_tool("file_exists", path)
    tool_results.append(verified)
    if not verified["success"] or not verified.get("exists"):
        return {
            "success": False,
            "task_id": "T2.3",
            "error": f"File verification failed for {path}",
            "tool_results": tool_results,
            "verified": False,
        }

    return {
        "success": True,
        "task_id": "T2.3",
        "files_created": [path],
        "verified": True,
        "tool_results": tool_results,
    }


ENEMY_HEALTH_SOURCE = '''using UnityEngine;
using UnityEngine.Events;

// Health component for an enemy; uses the IDamageable interface already defined by PlayerWeapon.cs.
public class EnemyHealth : MonoBehaviour, IDamageable
{
    [SerializeField, Min(1f)] private float maxHealth = 50f;
    [SerializeField] private UnityEvent onDeath;

    public float CurrentHealth { get; private set; }
    public bool IsDead { get; private set; }

    private void Awake()
    {
        CurrentHealth = maxHealth;
        IsDead = false;
    }

    public void TakeDamage(float amount)
    {
        if (IsDead || amount <= 0f)
            return;

        CurrentHealth = Mathf.Max(0f, CurrentHealth - amount);
        if (CurrentHealth <= 0f)
            Die();
    }

    private void Die()
    {
        // Set the state before notifying listeners so death is handled only once.
        if (IsDead)
            return;

        IsDead = true;
        onDeath?.Invoke();
    }
}
'''


ENEMY_CONTROLLER_SOURCE = '''using UnityEngine;

// Pursues a player target and applies close-range damage through PlayerHealth.
[RequireComponent(typeof(EnemyHealth))]
public class EnemyController : MonoBehaviour
{
    [SerializeField] private Transform playerTarget;
    [SerializeField, Min(0f)] private float movementSpeed = 3f;
    [SerializeField, Min(0.1f)] private float attackRange = 1.5f;
    [SerializeField, Min(0f)] private float attackDamage = 10f;
    [SerializeField, Min(0.01f)] private float attackCooldown = 1.5f;

    private EnemyHealth enemyHealth;
    private PlayerHealth playerHealth;
    private float nextAttackTime;

    private void Awake()
    {
        enemyHealth = GetComponent<EnemyHealth>();
        FindPlayerTarget();
    }

    private void Update()
    {
        // A defeated enemy remains inert and cannot chase or attack again.
        if (enemyHealth == null || enemyHealth.IsDead || playerTarget == null)
            return;

        if (playerHealth == null)
            playerHealth = playerTarget.GetComponentInParent<PlayerHealth>();
        if (playerHealth == null || playerHealth.IsDead)
            return;

        Vector3 offset = playerTarget.position - transform.position;
        offset.y = 0f;
        float distance = offset.magnitude;

        if (distance > attackRange)
        {
            Vector3 direction = offset.normalized;
            transform.position += direction * movementSpeed * Time.deltaTime;
            if (direction.sqrMagnitude > 0.001f)
                transform.rotation = Quaternion.LookRotation(direction);
            return;
        }

        if (Time.time >= nextAttackTime)
        {
            playerHealth.TakeDamage(attackDamage);
            nextAttackTime = Time.time + Mathf.Max(attackCooldown, 0.01f);
        }
    }

    private void FindPlayerTarget()
    {
        if (playerTarget == null)
        {
            GameObject playerObject = GameObject.FindGameObjectWithTag("Player");
            if (playerObject != null)
                playerTarget = playerObject.transform;
        }

        if (playerTarget != null)
            playerHealth = playerTarget.GetComponentInParent<PlayerHealth>();
    }
}
'''


def execute_t3_1(state: AgentState) -> dict[str, Any]:
    """Inspect existing player contracts, then create and verify enemy scripts."""
    inspected_paths = [
        "UnityProject/Assets/Scripts/PlayerWeapon.cs",
        "UnityProject/Assets/Scripts/PlayerHealth.cs",
        "UnityProject/Assets/Scripts/PlayerController.cs",
    ]
    tool_results = []
    inspected = {}
    for path in inspected_paths:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result["success"]:
            return {
                "success": False,
                "task_id": "T3.1",
                "error": f"Could not inspect required project file {path}: {result.get('error', 'read failed')}",
                "tool_results": tool_results,
                "verified": False,
            }
        inspected[path] = result["content"]

    weapon_source = inspected[inspected_paths[0]]
    player_health_source = inspected[inspected_paths[1]]
    # Generate against actual source signatures and refuse to create conflicting assumptions.
    if "public interface IDamageable" not in weapon_source or "void TakeDamage(float amount)" not in weapon_source:
        return {
            "success": False,
            "task_id": "T3.1",
            "error": "PlayerWeapon.cs does not expose the expected IDamageable.TakeDamage(float amount) contract",
            "tool_results": tool_results,
            "verified": False,
        }
    if "void TakeDamage(float amount)" not in player_health_source or "bool IsDead" not in player_health_source:
        return {
            "success": False,
            "task_id": "T3.1",
            "error": "PlayerHealth.cs does not expose the expected TakeDamage(float) and IsDead members",
            "tool_results": tool_results,
            "verified": False,
        }

    file_sources = [
        ("UnityProject/Assets/Scripts/EnemyHealth.cs", ENEMY_HEALTH_SOURCE),
        ("UnityProject/Assets/Scripts/EnemyController.cs", ENEMY_CONTROLLER_SOURCE),
    ]
    created_files = []
    for path, source in file_sources:
        created = _run_tool("create_file", path, source)
        tool_results.append(created)
        if not created["success"]:
            return {
                "success": False,
                "task_id": "T3.1",
                "error": f"Could not create {path}: {created.get('error', 'file creation failed')}",
                "files_created": created_files,
                "tool_results": tool_results,
                "verified": False,
            }

        verified = _run_tool("file_exists", path)
        tool_results.append(verified)
        if not verified["success"] or not verified.get("exists"):
            return {
                "success": False,
                "task_id": "T3.1",
                "error": f"File verification failed for {path}",
                "files_created": created_files,
                "tool_results": tool_results,
                "verified": False,
            }
        created_files.append(path)

    return {
        "success": True,
        "task_id": "T3.1",
        "files_created": created_files,
        "verified": True,
        "tool_results": tool_results,
    }


WAVE_MANAGER_SOURCE = '''using System.Collections;
using System.Collections.Generic;
using UnityEngine;

// Starts waves, spawns configured enemy prefabs, and tracks their existing EnemyHealth state.
public class WaveManager : MonoBehaviour
{
    [SerializeField] private GameObject enemyPrefab;
    [SerializeField] private Transform[] spawnPoints;
    [SerializeField, Min(1)] private int startingEnemies = 3;
    [SerializeField, Min(0)] private int additionalEnemiesPerWave = 2;
    [SerializeField, Min(0f)] private float timeBetweenWaves = 3f;
    [SerializeField, Min(0f)] private float spawnInterval = 0.5f;
    [SerializeField, Min(0)] private int maximumWaves = 0; // Zero means no limit.

    private readonly List<EnemyHealth> activeEnemies = new List<EnemyHealth>();
    private bool spawnFailed;

    public int CurrentWave { get; private set; }
    public int ActiveEnemyCount => activeEnemies.Count;

    private void Start()
    {
        // A missing prefab is a configuration error; stop cleanly instead of crashing.
        if (enemyPrefab == null)
        {
            Debug.LogError("WaveManager requires an enemy prefab reference.", this);
            return;
        }

        StartCoroutine(RunWaves());
    }

    private IEnumerator RunWaves()
    {
        while (maximumWaves <= 0 || CurrentWave < maximumWaves)
        {
            CurrentWave++;
            int enemyCount = Mathf.Max(1,
                startingEnemies + (CurrentWave - 1) * additionalEnemiesPerWave);

            Debug.Log($"Starting wave {CurrentWave} with {enemyCount} enemies.", this);
            yield return StartCoroutine(SpawnWave(enemyCount));
            if (spawnFailed)
                yield break;

            // A wave clears only after every expected spawn is made and all spawned enemies die.
            while (activeEnemies.Count > 0)
            {
                RemoveDeadOrDestroyedEnemies();
                if (activeEnemies.Count > 0)
                    yield return null;
            }

            Debug.Log($"Wave {CurrentWave} cleared.", this);
            if (maximumWaves > 0 && CurrentWave >= maximumWaves)
                yield break;

            yield return new WaitForSeconds(timeBetweenWaves);
        }
    }

    private IEnumerator SpawnWave(int enemyCount)
    {
        for (int i = 0; i < enemyCount; i++)
        {
            Transform point = GetSpawnPoint(i);
            GameObject spawned = Instantiate(enemyPrefab, point.position, point.rotation);
            EnemyHealth health = spawned.GetComponentInChildren<EnemyHealth>();

            if (health == null)
            {
                Debug.LogError("Spawned enemy prefab has no EnemyHealth component; destroying the invalid instance.", spawned);
                spawnFailed = true;
                Destroy(spawned);
                yield break;
            }
            else
            {
                activeEnemies.Add(health);
            }

            if (spawnInterval > 0f && i < enemyCount - 1)
                yield return new WaitForSeconds(spawnInterval);
        }
    }

    private Transform GetSpawnPoint(int spawnIndex)
    {
        // With no configured points, this manager's own transform is a safe fallback.
        if (spawnPoints == null || spawnPoints.Length == 0)
            return transform;

        int index = spawnIndex % spawnPoints.Length;
        if (spawnPoints[index] != null)
            return spawnPoints[index];

        // A null slot also falls back safely rather than throwing a null-reference error.
        return transform;
    }

    private void RemoveDeadOrDestroyedEnemies()
    {
        for (int i = activeEnemies.Count - 1; i >= 0; i--)
        {
            EnemyHealth enemy = activeEnemies[i];
            if (enemy == null || enemy.IsDead)
                activeEnemies.RemoveAt(i);
        }
    }
}
'''


def execute_t3_2(state: AgentState) -> dict[str, Any]:
    """Inspect existing components before creating a compatible wave manager."""
    inspected_paths = [
        "UnityProject/Assets/Scripts/EnemyController.cs",
        "UnityProject/Assets/Scripts/EnemyHealth.cs",
        "UnityProject/Assets/Scripts/PlayerHealth.cs",
    ]
    tool_results = []
    inspected = {}
    for path in inspected_paths:
        result = TOOLS["read_file"](path)
        print("\n========== CONTEXT INSPECTION ==========")
        print("\nTool: read_file")
        print(f"Path: {path}")
        print(f"Result: {'SUCCESS' if result['success'] else 'FAILURE'}")
        if result.get("error"):
            print(f"Reason: {result['error']}")
        tool_results.append(result)
        if not result["success"]:
            return {
                "success": False,
                "task_id": "T3.2",
                "error": f"Required context inspection failed for {path}: {result.get('error', 'read failed')}",
                "tool_results": tool_results,
                "verified": False,
            }
        inspected[path] = result["content"]

    enemy_controller = inspected[inspected_paths[0]]
    enemy_health = inspected[inspected_paths[1]]
    player_health = inspected[inspected_paths[2]]
    # Confirm the source APIs used by the generated manager match the files on disk.
    if "class EnemyController" not in enemy_controller:
        error = "EnemyController.cs does not define the expected EnemyController component"
    elif "class EnemyHealth" not in enemy_health or "bool IsDead" not in enemy_health:
        error = "EnemyHealth.cs does not expose the expected EnemyHealth.IsDead state"
    elif "class PlayerHealth" not in player_health or "void TakeDamage(float amount)" not in player_health:
        error = "PlayerHealth.cs does not expose the observed player health API"
    else:
        error = None

    if error:
        return {
            "success": False,
            "task_id": "T3.2",
            "error": error,
            "tool_results": tool_results,
            "verified": False,
        }

    path = "UnityProject/Assets/Scripts/WaveManager.cs"
    created = _run_tool("create_file", path, WAVE_MANAGER_SOURCE)
    tool_results.append(created)
    if not created["success"]:
        return {
            "success": False,
            "task_id": "T3.2",
            "error": created.get("error", "WaveManager.cs creation failed"),
            "tool_results": tool_results,
            "verified": False,
        }

    verified = _run_tool("file_exists", path)
    tool_results.append(verified)
    if not verified["success"] or not verified.get("exists"):
        return {
            "success": False,
            "task_id": "T3.2",
            "error": f"File verification failed for {path}",
            "tool_results": tool_results,
            "verified": False,
        }

    return {
        "success": True,
        "task_id": "T3.2",
        "files_created": [path],
        "files_verified": [path],
        "verified": True,
        "tool_results": tool_results,
    }


SCORE_MANAGER_SOURCE = '''using System;
using UnityEngine;

// Owns the current run score and exposes an event for future HUD listeners.
public class ScoreManager : MonoBehaviour
{
    public int CurrentScore { get; private set; }
    public event Action<int> ScoreChanged;

    public void AwardPoints(int points)
    {
        if (points <= 0)
            return;

        CurrentScore += points;
        ScoreChanged?.Invoke(CurrentScore);
    }
}
'''


def _replace_once(source: str, old: str, new: str, label: str) -> tuple[str, str | None]:
    """Apply one source edit only when the inspected code has the expected form."""
    if source.count(old) != 1:
        return source, f"Could not safely update {label}: expected source fragment was not unique."
    return source.replace(old, new, 1), None


def _ensure_fragment(source: str, old: str, new: str, label: str) -> tuple[str, str | None]:
    """Make a narrow source edit idempotently when the generated fragment exists."""
    if new in source:
        return source, None
    return _replace_once(source, old, new, label)


def execute_t3_3(state: AgentState) -> dict[str, Any]:
    """Inspect, implement, deploy, verify, and compile the score/wave-clear APIs."""
    task_id = "T3.3"
    tool_results: list[dict[str, Any]] = []
    inspected_paths = [
        "UnityProject/Assets/Scripts/WaveManager.cs",
        "UnityProject/Assets/Scripts/EnemyHealth.cs",
        "UnityProject/Assets/Scripts/PlayerWeapon.cs",
        "UnityProject/Assets/Scripts/EnemyController.cs",
    ]
    inspected: dict[str, str] = {}
    for path in inspected_paths:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result["success"]:
            return {
                "success": False,
                "task_id": task_id,
                "error": f"Could not inspect required source {path}: {result.get('error', 'read failed')}",
                "tool_results": tool_results,
                "verified": False,
            }
        inspected[path] = result["content"]

    wave_path, health_path, weapon_path, controller_path = inspected_paths
    wave_source = inspected[wave_path]
    health_source = inspected[health_path]
    weapon_source = inspected[weapon_path]
    controller_source = inspected[controller_path]
    observed_contracts = (
        "public int CurrentWave { get; private set; }" in wave_source
        and "private readonly List<EnemyHealth> activeEnemies" in wave_source
        and "Wave {CurrentWave} cleared." in wave_source
        and "public bool IsDead { get; private set; }" in health_source
        and "IsDead = true;" in health_source
        and "public interface IDamageable" in weapon_source
        and "damageable.TakeDamage(damage);" in weapon_source
        and "enemyHealth.IsDead" in controller_source
    )
    if not observed_contracts:
        return {
            "success": False,
            "task_id": task_id,
            "error": "Inspected script APIs differ from the expected T3.1/T3.2 contracts; refusing to overwrite them.",
            "tool_results": tool_results,
            "verified": False,
        }

    # Add a C# event to the existing one-shot death path while preserving its UnityEvent.
    health_updated, error = _replace_once(
        health_source,
        "using UnityEngine;\n",
        "using System;\nusing UnityEngine;\n",
        "EnemyHealth imports",
    )
    if not error:
        health_updated, error = _replace_once(
            health_updated,
            "    public bool IsDead { get; private set; }\n",
            "    public bool IsDead { get; private set; }\n"
            "    public event Action<EnemyHealth> Died;\n",
            "EnemyHealth death event",
        )
    if not error:
        health_updated, error = _replace_once(
            health_updated,
            "        IsDead = true;\n        onDeath?.Invoke();",
            "        IsDead = true;\n        Died?.Invoke(this);\n        onDeath?.Invoke();",
            "EnemyHealth death notification",
        )
    if error:
        return {"success": False, "task_id": task_id, "error": error,
                "tool_results": tool_results, "verified": False}

    wave_updated, error = _replace_once(
        wave_source,
        "using System.Collections;\n",
        "using System;\nusing System.Collections;\n",
        "WaveManager imports",
    )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "    [SerializeField, Min(0)] private int maximumWaves = 0; // Zero means no limit.\n",
            "    [SerializeField, Min(0)] private int maximumWaves = 0; // Zero means no limit.\n"
            "    [SerializeField, Min(1)] private int pointsPerEnemy = 100;\n"
            "    [SerializeField] private ScoreManager scoreManager;\n",
            "WaveManager score configuration",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "    private readonly List<EnemyHealth> activeEnemies = new List<EnemyHealth>();\n",
            "    private readonly List<EnemyHealth> activeEnemies = new List<EnemyHealth>();\n"
            "    private readonly HashSet<EnemyHealth> scoredEnemies = new HashSet<EnemyHealth>();\n",
            "WaveManager duplicate-award guard",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "    public int CurrentWave { get; private set; }\n",
            "    public int CurrentWave { get; private set; }\n"
            "    public event Action<int> WaveCleared;\n",
            "WaveManager clear event",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "    private void Start()\n",
            "    private void Awake()\n"
            "    {\n"
            "        if (scoreManager == null)\n"
            "            scoreManager = GetComponent<ScoreManager>();\n"
            "        if (scoreManager == null)\n"
            "            scoreManager = gameObject.AddComponent<ScoreManager>();\n"
            "    }\n\n"
            "    private void Start()\n",
            "WaveManager score manager setup",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            '            Debug.Log($"Wave {CurrentWave} cleared.", this);',
            '            Debug.Log($"Wave {CurrentWave} cleared.", this);\n'
            "            WaveCleared?.Invoke(CurrentWave);",
            "WaveManager clear notification",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "                activeEnemies.Add(health);",
            "                activeEnemies.Add(health);\n"
            "                health.Died += HandleEnemyDied;",
            "WaveManager enemy death subscription",
        )
    if not error:
        wave_updated, error = _replace_once(
            wave_updated,
            "    private Transform GetSpawnPoint(int spawnIndex)\n",
            "    private void HandleEnemyDied(EnemyHealth enemy)\n"
            "    {\n"
            "        if (enemy == null || !scoredEnemies.Add(enemy))\n"
            "            return;\n\n"
            "        if (scoreManager != null)\n"
            "            scoreManager.AwardPoints(pointsPerEnemy);\n"
            "    }\n\n"
            "    private Transform GetSpawnPoint(int spawnIndex)\n",
            "WaveManager death score handler",
        )
    if error:
        return {"success": False, "task_id": task_id, "error": error,
                "tool_results": tool_results, "verified": False}

    file_sources = [
        ("UnityProject/Assets/Scripts/EnemyHealth.cs", health_updated, "modified"),
        ("UnityProject/Assets/Scripts/WaveManager.cs", wave_updated, "modified"),
        ("UnityProject/Assets/Scripts/ScoreManager.cs", SCORE_MANAGER_SOURCE, "created"),
    ]
    files_created: list[str] = []
    files_modified: list[str] = []
    for path, source, action in file_sources:
        result = _run_tool("create_file", path, source)
        tool_results.append(result)
        if not result["success"]:
            return {"success": False, "task_id": task_id,
                    "error": f"Could not write {path}: {result.get('error', 'write failed')}",
                    "files_created": files_created, "files_modified": files_modified,
                    "tool_results": tool_results, "verified": False}
        (files_created if action == "created" else files_modified).append(path)

        readback = _run_tool("read_file", path)
        tool_results.append(readback)
        if not readback["success"] or readback.get("content") != source:
            return {"success": False, "task_id": task_id,
                    "error": f"Workspace source verification failed for {path}",
                    "files_created": files_created, "files_modified": files_modified,
                    "tool_results": tool_results, "verified": False}

    deployed: list[str] = []
    verified: list[str] = []
    for path, source, _ in file_sources:
        relative_path = "Assets/Scripts/" + Path(path).name
        deployment = TOOLS["deploy_script_to_unity"](relative_path, source)
        print("\n========== TOOL EXECUTION ==========")
        print("\nTool: deploy_script_to_unity")
        print(f"Path: {relative_path}")
        print(f"Result: {'SUCCESS' if deployment.get('success') else 'FAILURE'}")
        if deployment.get("error"):
            print(f"Reason: {deployment['error']}")
        tool_results.append(deployment)
        if not deployment.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Unity deployment failed for {relative_path}: {deployment.get('error')}",
                    "files_created": files_created, "files_modified": files_modified,
                    "files_deployed": deployed, "tool_results": tool_results, "verified": False}
        deployed.append(relative_path)

        existence = TOOLS["unity_file_exists"](relative_path)
        readback = TOOLS["unity_read_file"](relative_path) if existence.get("exists") else None
        if (not existence.get("success") or not existence.get("exists") or not readback
                or not readback.get("success") or readback.get("content") != source):
            return {"success": False, "task_id": task_id,
                    "error": f"Unity deployment read-back verification failed for {relative_path}",
                    "files_created": files_created, "files_modified": files_modified,
                    "files_deployed": deployed, "files_verified": verified,
                    "tool_results": tool_results, "verified": False}
        verified.append(relative_path)

    compilation = TOOLS["validate_unity_compilation"]()
    tool_results.append({"tool": "validate_unity_compilation", **compilation})
    print("\nUnity T3.3 compilation:")
    print(f"  Exit code: {compilation.get('exit_code')}")
    print(f"  C# compiler errors: {len(compilation.get('compiler_errors', []))}")
    print(f"  Compilation: {'SUCCESS' if compilation.get('success') else 'FAIL'}")
    for diagnostic in compilation.get("compiler_errors", []):
        print(f"  Compiler error: {diagnostic}")
    for message in compilation.get("unity_messages", []):
        print(f"  Generic Unity message: {message}")

    success = (len(deployed) == len(file_sources) and len(verified) == len(file_sources)
               and compilation.get("exit_code") == 0
               and not compilation.get("compiler_errors"))
    return {
        "success": success,
        "task_id": task_id,
        "files_created": files_created,
        "files_modified": files_modified,
        "files_deployed": deployed,
        "files_verified": verified,
        "compilation": compilation,
        "verified": success,
        "error": None if success else "T3.3 Unity deployment or compilation verification failed.",
        "tool_results": tool_results,
    }


GAME_HUD_SOURCE = '''using System.Collections;
using UnityEngine;
using UnityEngine.UI;

// Runtime-created, non-interactive HUD. It binds safely as gameplay components appear.
public class GameHUD : MonoBehaviour
{
    private const float RefreshIntervalSeconds = 0.25f;

    private PlayerHealth playerHealth;
    private PlayerWeapon playerWeapon;
    private WaveManager waveManager;
    private ScoreManager scoreManager;
    private Text healthText;
    private Text ammoText;
    private Text waveText;
    private Text scoreText;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    private static void CreateHudAfterSceneLoad()
    {
        if (FindFirstObjectByType<GameHUD>() != null)
            return;

        GameObject hudObject = new GameObject("GameHUD");
        DontDestroyOnLoad(hudObject);
        hudObject.AddComponent<GameHUD>();
    }

    private void Awake()
    {
        BuildHud();
    }

    private void OnEnable()
    {
        StartCoroutine(RefreshRoutine());
    }

    private void OnDisable()
    {
        StopAllCoroutines();
        UnsubscribeFromEvents();
    }

    private IEnumerator RefreshRoutine()
    {
        WaitForSeconds wait = new WaitForSeconds(RefreshIntervalSeconds);
        while (true)
        {
            RefreshBindings();
            RefreshLabels();
            yield return wait;
        }
    }

    private void BuildHud()
    {
        GameObject canvasObject = new GameObject("HUD Canvas");
        canvasObject.transform.SetParent(transform, false);

        Canvas canvas = canvasObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        canvas.sortingOrder = 10;
        CanvasScaler scaler = canvasObject.AddComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.matchWidthOrHeight = 0.5f;
        canvasObject.AddComponent<GraphicRaycaster>();

        GameObject panel = new GameObject("HUD Panel");
        panel.transform.SetParent(canvasObject.transform, false);
        Image background = panel.AddComponent<Image>();
        background.color = new Color(0f, 0f, 0f, 0.6f);
        RectTransform panelRect = panel.GetComponent<RectTransform>();
        panelRect.anchorMin = new Vector2(0f, 1f);
        panelRect.anchorMax = new Vector2(0f, 1f);
        panelRect.pivot = new Vector2(0f, 1f);
        panelRect.anchoredPosition = new Vector2(18f, -18f);
        panelRect.sizeDelta = new Vector2(250f, 150f);

        healthText = CreateLabel(panel.transform, "Health Label", 0);
        ammoText = CreateLabel(panel.transform, "Ammo Label", 1);
        waveText = CreateLabel(panel.transform, "Wave Label", 2);
        scoreText = CreateLabel(panel.transform, "Score Label", 3);
    }

    private Text CreateLabel(Transform parent, string objectName, int row)
    {
        GameObject labelObject = new GameObject(objectName);
        labelObject.transform.SetParent(parent, false);
        Text label = labelObject.AddComponent<Text>();
        label.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        label.fontSize = 20;
        label.color = Color.white;
        label.alignment = TextAnchor.MiddleLeft;
        label.horizontalOverflow = HorizontalWrapMode.Overflow;
        label.verticalOverflow = VerticalWrapMode.Truncate;
        label.raycastTarget = false;

        RectTransform rect = label.rectTransform;
        rect.anchorMin = new Vector2(0f, 1f);
        rect.anchorMax = new Vector2(1f, 1f);
        rect.pivot = new Vector2(0.5f, 1f);
        rect.anchoredPosition = new Vector2(10f, -10f - row * 34f);
        rect.sizeDelta = new Vector2(-20f, 30f);
        return label;
    }

    private void RefreshBindings()
    {
        PlayerHealth foundHealth = FindFirstObjectByType<PlayerHealth>();
        PlayerWeapon foundWeapon = FindFirstObjectByType<PlayerWeapon>();
        WaveManager foundWaveManager = FindFirstObjectByType<WaveManager>();
        ScoreManager foundScoreManager = FindFirstObjectByType<ScoreManager>();

        if (waveManager != foundWaveManager)
        {
            if (waveManager != null)
                waveManager.WaveCleared -= HandleWaveCleared;
            waveManager = foundWaveManager;
            if (waveManager != null)
                waveManager.WaveCleared += HandleWaveCleared;
        }

        if (scoreManager != foundScoreManager)
        {
            if (scoreManager != null)
                scoreManager.ScoreChanged -= HandleScoreChanged;
            scoreManager = foundScoreManager;
            if (scoreManager != null)
                scoreManager.ScoreChanged += HandleScoreChanged;
        }

        playerHealth = foundHealth;
        playerWeapon = foundWeapon;
    }

    private void RefreshLabels()
    {
        if (healthText != null)
            healthText.text = playerHealth != null
                ? $"Health: {playerHealth.CurrentHealth:0}/{playerHealth.MaxHealth:0}"
                : "Health: --";

        if (ammoText != null)
            ammoText.text = playerWeapon != null
                ? $"Ammo: {playerWeapon.CurrentAmmo}/{playerWeapon.MagazineSize}"
                    + (playerWeapon.IsReloading ? " (Reloading)" : string.Empty)
                : "Ammo: --";

        if (waveText != null)
            waveText.text = waveManager != null ? $"Wave: {waveManager.CurrentWave}" : "Wave: --";

        if (scoreText != null)
            scoreText.text = scoreManager != null ? $"Score: {scoreManager.CurrentScore}" : "Score: --";
    }

    private void HandleWaveCleared(int clearedWave)
    {
        RefreshLabels();
    }

    private void HandleScoreChanged(int newScore)
    {
        RefreshLabels();
    }

    private void UnsubscribeFromEvents()
    {
        if (waveManager != null)
            waveManager.WaveCleared -= HandleWaveCleared;
        if (scoreManager != null)
            scoreManager.ScoreChanged -= HandleScoreChanged;
    }
}
'''


def execute_t4_1(state: AgentState) -> dict[str, Any]:
    """Inspect gameplay contracts, then deploy and compile the runtime HUD."""
    task_id = "T4.1"
    tool_results: list[dict[str, Any]] = []
    inspected_paths = [
        "UnityProject/Assets/Scripts/PlayerHealth.cs",
        "UnityProject/Assets/Scripts/PlayerWeapon.cs",
        "UnityProject/Assets/Scripts/WaveManager.cs",
        "UnityProject/Assets/Scripts/ScoreManager.cs",
    ]
    inspected: dict[str, str] = {}
    for path in inspected_paths:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result["success"]:
            return {
                "success": False,
                "task_id": task_id,
                "error": f"Could not inspect required source {path}: {result.get('error', 'read failed')}",
                "tool_results": tool_results,
                "verified": False,
            }
        inspected[path] = result["content"]

    health, weapon, waves, score = (inspected[path] for path in inspected_paths)
    api_checks = {
        "health": "public float CurrentHealth { get; private set; }" in health
                  and "public float MaxHealth => maxHealth;" in health,
        "ammo": "public int CurrentAmmo => currentAmmo;" in weapon
                and "public int MagazineSize => magazineSize;" in weapon
                and "public bool IsReloading => isReloading;" in weapon,
        "wave": "public int CurrentWave { get; private set; }" in waves
                and "public event Action<int> WaveCleared;" in waves,
        "score": "public int CurrentScore { get; private set; }" in score
                 and "public event Action<int> ScoreChanged;" in score,
    }
    if not all(api_checks.values()):
        return {
            "success": False,
            "task_id": task_id,
            "error": f"Inspected gameplay APIs did not match the HUD binding contract: {api_checks}",
            "tool_results": tool_results,
            "verified": False,
        }

    manifest = TOOLS["unity_read_file"]("Packages/manifest.json")
    if not manifest.get("success"):
        return {"success": False, "task_id": task_id,
                "error": f"Could not inspect Unity package manifest: {manifest.get('error')}",
                "tool_results": tool_results, "verified": False}
    if '"com.unity.ugui"' not in manifest.get("content", ""):
        return {"success": False, "task_id": task_id,
                "error": "Unity UGUI is not installed; refusing to generate a HUD with an unavailable UI package.",
                "tool_results": tool_results, "verified": False}
    tool_results.append({"tool": "unity_read_file", **manifest})

    workspace_path = "UnityProject/Assets/Scripts/GameHUD.cs"
    create = _run_tool("create_file", workspace_path, GAME_HUD_SOURCE)
    tool_results.append(create)
    if not create["success"]:
        return {"success": False, "task_id": task_id,
                "error": f"Could not create GameHUD.cs: {create.get('error', 'write failed')}",
                "files_created": [], "tool_results": tool_results, "verified": False}

    workspace_readback = _run_tool("read_file", workspace_path)
    tool_results.append(workspace_readback)
    if (not workspace_readback["success"]
            or workspace_readback.get("content") != GAME_HUD_SOURCE):
        return {"success": False, "task_id": task_id,
                "error": "Workspace GameHUD.cs read-back verification failed.",
                "files_created": [workspace_path], "tool_results": tool_results,
                "verified": False}

    relative_path = "Assets/Scripts/GameHUD.cs"
    deployment = TOOLS["deploy_script_to_unity"](relative_path, GAME_HUD_SOURCE)
    tool_results.append(deployment)
    if not deployment.get("success"):
        return {"success": False, "task_id": task_id,
                "error": f"Unity deployment failed: {deployment.get('error')}",
                "files_created": [workspace_path], "files_deployed": [],
                "tool_results": tool_results, "verified": False}

    existence = TOOLS["unity_file_exists"](relative_path)
    deployed_readback = (TOOLS["unity_read_file"](relative_path)
                         if existence.get("success") and existence.get("exists") else None)
    if (not existence.get("success") or not existence.get("exists")
            or deployed_readback is None or not deployed_readback.get("success")
            or deployed_readback.get("content") != GAME_HUD_SOURCE):
        return {"success": False, "task_id": task_id,
                "error": "Deployed GameHUD.cs read-back verification failed.",
                "files_created": [workspace_path], "files_deployed": [relative_path],
                "files_verified": [], "tool_results": tool_results, "verified": False}
    tool_results.extend([existence, deployed_readback])

    compilation = TOOLS["validate_unity_compilation"]()
    tool_results.append({"tool": "validate_unity_compilation", **compilation})
    success = (compilation.get("exit_code") == 0
               and not compilation.get("compiler_errors"))
    print("\nUnity T4.1 HUD compilation:")
    print(f"  Exit code: {compilation.get('exit_code')}")
    print(f"  C# compiler errors: {len(compilation.get('compiler_errors', []))}")
    print(f"  Compilation: {'SUCCESS' if success else 'FAIL'}")
    for diagnostic in compilation.get("compiler_errors", []):
        print(f"  Compiler error: {diagnostic}")
    for message in compilation.get("unity_messages", []):
        print(f"  Generic Unity message: {message}")

    return {
        "success": success,
        "task_id": task_id,
        "files_created": [workspace_path],
        "files_modified": [],
        "scene_objects_created": [
            "Runtime GameHUD root",
            "HUD Canvas",
            "HUD Panel",
            "Health Label",
            "Ammo Label",
            "Wave Label",
            "Score Label",
        ],
        "scene_objects_modified": [],
        "scene_asset_modified": False,
        "files_deployed": [relative_path],
        "files_verified": [relative_path],
        "binding_contract_verified": api_checks,
        "compilation": compilation,
        "verified": success,
        "error": None if success else "T4.1 deployment or Unity compilation verification failed.",
        "tool_results": tool_results,
    }


GAME_STATE_MANAGER_SOURCE = '''using System;
using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;

public enum GameState
{
    Start,
    Playing,
    Paused,
    GameOver
}

// Owns high-level game state while scene-bound gameplay systems retain their own data.
public class GameStateManager : MonoBehaviour
{
    private static GameStateManager instance;
    private bool restartInProgress;
    private PlayerHealth playerHealth;
    private WaveManager waveManager;

    public static GameStateManager Instance => instance;
    public GameState CurrentState { get; private set; } = GameState.Start;
    public bool IsGameplayRunning => CurrentState == GameState.Playing;
    public event Action<GameState> StateChanged;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    private static void CreateManagerAfterSceneLoad()
    {
        if (FindFirstObjectByType<GameStateManager>() != null)
            return;

        GameObject managerObject = new GameObject("GameStateManager");
        DontDestroyOnLoad(managerObject);
        managerObject.AddComponent<GameStateManager>();
    }

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
    private static void ResetStatics()
    {
        instance = null;
    }

    private void Awake()
    {
        if (instance != null && instance != this)
        {
            Destroy(gameObject);
            return;
        }

        instance = this;
        DontDestroyOnLoad(gameObject);
        CurrentState = GameState.Start;
        Time.timeScale = 0f;
        BindGameplayReferences();
    }

    private void OnEnable()
    {
        SceneManager.sceneLoaded += HandleSceneLoaded;
        StartCoroutine(BindGameplayReferencesRoutine());
    }

    private void OnDisable()
    {
        SceneManager.sceneLoaded -= HandleSceneLoaded;
        StopAllCoroutines();
        if (playerHealth != null)
            playerHealth.Died -= HandlePlayerDied;
    }

    private IEnumerator BindGameplayReferencesRoutine()
    {
        WaitForSecondsRealtime wait = new WaitForSecondsRealtime(0.25f);
        while (true)
        {
            BindGameplayReferences();
            yield return wait;
        }
    }

    public void StartGame()
    {
        if (CurrentState != GameState.Start)
            return;

        Time.timeScale = 1f;
        SetState(GameState.Playing);
        BindGameplayReferences();
        if (CurrentState == GameState.Playing && waveManager != null)
            waveManager.BeginWaves();
    }

    public void Pause()
    {
        if (CurrentState != GameState.Playing)
            return;

        Time.timeScale = 0f;
        SetState(GameState.Paused);
    }

    public void Resume()
    {
        if (CurrentState != GameState.Paused)
            return;

        Time.timeScale = 1f;
        SetState(GameState.Playing);
        BindGameplayReferences();
        if (waveManager != null)
            waveManager.BeginWaves();
    }

    public void TogglePause()
    {
        if (CurrentState == GameState.Playing)
            Pause();
        else if (CurrentState == GameState.Paused)
            Resume();
    }

    public void Restart()
    {
        if (restartInProgress)
            return;

        Scene activeScene = SceneManager.GetActiveScene();
        if (!activeScene.IsValid() || activeScene.buildIndex < 0)
        {
            Debug.LogError("Cannot restart because the active scene is not in Build Settings.", this);
            return;
        }

        restartInProgress = true;
        Time.timeScale = 1f;
        SceneManager.LoadScene(activeScene.buildIndex);
    }

    private void BindGameplayReferences()
    {
        PlayerHealth foundHealth = FindFirstObjectByType<PlayerHealth>();
        if (playerHealth != foundHealth)
        {
            if (playerHealth != null)
                playerHealth.Died -= HandlePlayerDied;
            playerHealth = foundHealth;
            if (playerHealth != null)
                playerHealth.Died += HandlePlayerDied;
        }

        WaveManager foundWaveManager = FindFirstObjectByType<WaveManager>();
        if (waveManager != foundWaveManager)
        {
            waveManager = foundWaveManager;
            if (CurrentState == GameState.Playing && waveManager != null)
                waveManager.BeginWaves();
        }
    }

    private void HandlePlayerDied()
    {
        if (CurrentState == GameState.GameOver)
            return;

        Time.timeScale = 0f;
        SetState(GameState.GameOver);
    }

    private void HandleSceneLoaded(Scene scene, LoadSceneMode mode)
    {
        restartInProgress = false;
        if (playerHealth != null)
            playerHealth.Died -= HandlePlayerDied;
        playerHealth = null;
        waveManager = null;

        Time.timeScale = 0f;
        SetState(GameState.Start);
        BindGameplayReferences();
    }

    private void SetState(GameState nextState)
    {
        if (CurrentState == nextState)
            return;

        CurrentState = nextState;
        StateChanged?.Invoke(CurrentState);
    }
}
'''


def execute_t4_2(state: AgentState) -> dict[str, Any]:
    """Inspect gameplay APIs, add the central state manager, deploy, and compile."""
    task_id = "T4.2"
    tool_results: list[dict[str, Any]] = []
    inspected_paths = [
        "UnityProject/Assets/Scripts/PlayerHealth.cs",
        "UnityProject/Assets/Scripts/PlayerController.cs",
        "UnityProject/Assets/Scripts/PlayerWeapon.cs",
        "UnityProject/Assets/Scripts/WaveManager.cs",
        "UnityProject/Assets/Scripts/ScoreManager.cs",
        "UnityProject/Assets/Scripts/GameHUD.cs",
    ]
    inspected: dict[str, str] = {}
    for path in inspected_paths:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result["success"]:
            return {"success": False, "task_id": task_id,
                    "error": f"Could not inspect {path}: {result.get('error', 'read failed')}",
                    "tool_results": tool_results, "verified": False}
        inspected[path] = result["content"]

    health_path, controller_path, weapon_path, wave_path, score_path, hud_path = inspected_paths
    health = inspected[health_path]
    controller = inspected[controller_path]
    weapon = inspected[weapon_path]
    waves = inspected[wave_path]
    score = inspected[score_path]
    hud = inspected[hud_path]
    api_checks = {
        "health_state": "public bool IsDead { get; private set; }" in health,
        "player_update": "private void Update()" in controller and "characterController.Move" in controller,
        "weapon_methods": "public void Fire()" in weapon and "public void Reload()" in weapon,
        "wave_current_state": "public int CurrentWave { get; private set; }" in waves,
        "wave_start_behavior": "StartCoroutine(RunWaves());" in waves,
        "score_reset_on_scene_recreation": "public int CurrentScore { get; private set; }" in score,
        "persistent_runtime_hud": "DontDestroyOnLoad(hudObject);" in hud,
    }
    if not all(api_checks.values()):
        return {"success": False, "task_id": task_id,
                "error": f"Inspected scripts differ from the expected game-state integration contracts: {api_checks}",
                "tool_results": tool_results, "verified": False}

    build_settings = TOOLS["unity_read_file"]("ProjectSettings/EditorBuildSettings.asset")
    if not build_settings.get("success"):
        return {"success": False, "task_id": task_id,
                "error": f"Could not verify restart scene configuration: {build_settings.get('error')}",
                "tool_results": tool_results, "verified": False}
    scene_result = TOOLS["unity_read_file"]("Assets/Scenes/SampleScene.unity")
    if not scene_result.get("success"):
        return {"success": False, "task_id": task_id,
                "error": f"Could not inspect the active game scene: {scene_result.get('error')}",
                "tool_results": tool_results, "verified": False}
    scene_path = "Assets/Scenes/SampleScene.unity"
    if scene_path not in build_settings.get("content", ""):
        return {"success": False, "task_id": task_id,
                "error": "SampleScene is not listed in Build Settings; safe scene-reload restart is unavailable.",
                "tool_results": tool_results, "verified": False}
    tool_results.extend([
        {"tool": "unity_read_file", **build_settings},
        {"tool": "unity_read_file", **scene_result},
    ])

    source_by_path = {
        health_path: health,
        controller_path: controller,
        weapon_path: weapon,
        wave_path: waves,
        "UnityProject/Assets/Scripts/EnemyController.cs": None,
    }
    enemy_controller_path = "UnityProject/Assets/Scripts/EnemyController.cs"
    enemy_controller_result = _run_tool("read_file", enemy_controller_path)
    tool_results.append(enemy_controller_result)
    if not enemy_controller_result["success"]:
        return {"success": False, "task_id": task_id,
                "error": f"Could not inspect {enemy_controller_path}: {enemy_controller_result.get('error')}",
                "tool_results": tool_results, "verified": False}
    source_by_path[enemy_controller_path] = enemy_controller_result["content"]

    # Extend observed components only where state control requires it.
    health_updated, error = _ensure_fragment(
        health,
        "using UnityEngine;\n",
        "using System;\nusing UnityEngine;\n",
        "PlayerHealth event import",
    )
    if not error:
        health_updated, error = _ensure_fragment(
            health_updated,
            "    public bool IsDead { get; private set; }\n",
            "    public bool IsDead { get; private set; }\n"
            "    public event Action Died;\n",
            "PlayerHealth death event",
        )
    if not error:
        health_updated, error = _ensure_fragment(
            health_updated,
            "        IsDead = true;\n        onDeath?.Invoke();",
            "        IsDead = true;\n        Died?.Invoke();\n        onDeath?.Invoke();",
            "PlayerHealth death event invocation",
        )
    if not error:
        health_updated, error = _ensure_fragment(
            health_updated,
            "    public void TakeDamage(float amount)\n    {\n",
            "    public void TakeDamage(float amount)\n    {\n"
            "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
            "            return;\n\n",
            "PlayerHealth state gate",
        )
    if error:
        return {"success": False, "task_id": task_id, "error": error,
                "tool_results": tool_results, "verified": False}

    controller_updated, error = _ensure_fragment(
        controller,
        "    private void Update()\n    {\n",
        "    private void Update()\n    {\n"
        "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
        "            return;\n\n",
        "PlayerController state gate",
    )
    weapon_updated = weapon
    if not error:
        weapon_updated, error = _ensure_fragment(
            weapon_updated,
            "    public void Fire()\n    {\n",
            "    public void Fire()\n    {\n"
            "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
            "            return;\n\n",
            "PlayerWeapon fire state gate",
        )
    if not error:
        weapon_updated, error = _ensure_fragment(
            weapon_updated,
            "    public void Reload()\n    {\n",
            "    public void Reload()\n    {\n"
            "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
            "            return;\n\n",
            "PlayerWeapon reload state gate",
        )

    enemy_controller = source_by_path[enemy_controller_path]
    enemy_controller_updated = enemy_controller
    if not error:
        enemy_controller_updated, error = _ensure_fragment(
            enemy_controller,
            "    private void Update()\n    {\n",
            "    private void Update()\n    {\n"
            "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
            "            return;\n\n",
            "EnemyController state gate",
        )

    wave_updated = waves
    if not error:
        wave_updated, error = _ensure_fragment(
            wave_updated,
            "    private bool spawnFailed;\n",
            "    private bool spawnFailed;\n    private bool wavesStarted;\n",
            "WaveManager start guard",
        )
    if not error:
        wave_updated, error = _ensure_fragment(
            wave_updated,
            "    private void Start()\n"
            "    {\n"
            "        // A missing prefab is a configuration error; stop cleanly instead of crashing.\n"
            "        if (enemyPrefab == null)\n"
            "        {\n"
            "            Debug.LogError(\"WaveManager requires an enemy prefab reference.\", this);\n"
            "            return;\n"
            "        }\n\n"
            "        StartCoroutine(RunWaves());\n"
            "    }\n",
            "    public void BeginWaves()\n"
            "    {\n"
            "        if (wavesStarted)\n"
            "            return;\n\n"
            "        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)\n"
            "            return;\n\n"
            "        // Do not start wave spawning until the central manager enters Playing.\n"
            "        if (enemyPrefab == null)\n"
            "        {\n"
            "            Debug.LogError(\"WaveManager requires an enemy prefab reference.\", this);\n"
            "            return;\n"
            "        }\n\n"
            "        wavesStarted = true;\n"
            "        StartCoroutine(RunWaves());\n"
            "    }\n",
            "WaveManager explicit start",
        )
    if error:
        return {"success": False, "task_id": task_id, "error": error,
                "tool_results": tool_results, "verified": False}

    file_sources = [
        (health_path, health_updated, "modified"),
        (controller_path, controller_updated, "modified"),
        (weapon_path, weapon_updated, "modified"),
        (enemy_controller_path, enemy_controller_updated, "modified"),
        (wave_path, wave_updated, "modified"),
        ("UnityProject/Assets/Scripts/GameStateManager.cs", GAME_STATE_MANAGER_SOURCE, "created"),
    ]
    files_created: list[str] = []
    files_modified: list[str] = []
    tool_results.append({"tool": "unity_read_file", **build_settings})
    for path, source, action in file_sources:
        create = _run_tool("create_file", path, source)
        tool_results.append(create)
        if not create["success"]:
            return {"success": False, "task_id": task_id,
                    "error": f"Could not write {path}: {create.get('error', 'write failed')}",
                    "files_created": files_created, "files_modified": files_modified,
                    "tool_results": tool_results, "verified": False}
        (files_created if action == "created" else files_modified).append(path)

        readback = _run_tool("read_file", path)
        tool_results.append(readback)
        if not readback["success"] or readback.get("content") != source:
            return {"success": False, "task_id": task_id,
                    "error": f"Workspace verification failed for {path}",
                    "files_created": files_created, "files_modified": files_modified,
                    "tool_results": tool_results, "verified": False}

    files_deployed: list[str] = []
    files_verified: list[str] = []
    for path, source, _ in file_sources:
        relative_path = "Assets/Scripts/" + Path(path).name
        deployment = TOOLS["deploy_script_to_unity"](relative_path, source)
        tool_results.append(deployment)
        if not deployment.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Unity deployment failed for {relative_path}: {deployment.get('error')}",
                    "files_created": files_created, "files_modified": files_modified,
                    "files_deployed": files_deployed, "files_verified": files_verified,
                    "tool_results": tool_results, "verified": False}
        files_deployed.append(relative_path)

        exists = TOOLS["unity_file_exists"](relative_path)
        readback = (TOOLS["unity_read_file"](relative_path)
                    if exists.get("success") and exists.get("exists") else None)
        if (not exists.get("success") or not exists.get("exists") or readback is None
                or not readback.get("success") or readback.get("content") != source):
            return {"success": False, "task_id": task_id,
                    "error": f"Unity read-back verification failed for {relative_path}",
                    "files_created": files_created, "files_modified": files_modified,
                    "files_deployed": files_deployed, "files_verified": files_verified,
                    "tool_results": tool_results, "verified": False}
        files_verified.append(relative_path)
        tool_results.extend([exists, readback])

    compilation = TOOLS["validate_unity_compilation"]()
    tool_results.append({"tool": "validate_unity_compilation", **compilation})
    success = (len(files_verified) == len(file_sources)
               and compilation.get("exit_code") == 0
               and not compilation.get("compiler_errors"))
    print("\nUnity T4.2 state-manager compilation:")
    print(f"  Exit code: {compilation.get('exit_code')}")
    print(f"  C# compiler errors: {len(compilation.get('compiler_errors', []))}")
    print(f"  Compilation: {'SUCCESS' if success else 'FAIL'}")
    for diagnostic in compilation.get("compiler_errors", []):
        print(f"  Compiler error: {diagnostic}")
    for message in compilation.get("unity_messages", []):
        print(f"  Generic Unity message: {message}")

    return {
        "success": success,
        "task_id": task_id,
        "files_created": files_created,
        "files_modified": files_modified,
        "files_deployed": files_deployed,
        "files_verified": files_verified,
        "restart_scene_verified": scene_path,
        "runtime_validation_performed": False,
        "compilation": compilation,
        "verified": success,
        "error": None if success else "T4.2 deployment or Unity compilation verification failed.",
        "tool_results": tool_results,
    }


T43_INSPECTION_SCRIPTS = (
    "WaveManager.cs", "EnemyController.cs", "EnemyHealth.cs", "PlayerHealth.cs",
    "PlayerWeapon.cs", "ScoreManager.cs", "GameStateManager.cs", "GameHUD.cs",
)
T43_INSPECTION_ASSETS = (
    "UnityProject/Assets/Scenes/SampleScene.unity",
    "UnityProject/Assets/Prefabs/Enemy.prefab",
)


def execute_t4_3(state: AgentState) -> dict[str, Any]:
    """Inspect existing gameplay, deploy an isolated test bridge, and validate three waves."""
    task_id = "T4.3"
    tool_results: list[dict[str, Any]] = []
    base = "UnityProject/Assets/Scripts/"
    inspected: dict[str, str] = {}
    for filename in T43_INSPECTION_SCRIPTS:
        path = base + filename
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Could not inspect {path}: {result.get('error')}",
                    "tool_results": tool_results, "verified": False}
        inspected[filename] = result["content"]

    inspected_assets: dict[str, str] = {}
    for path in T43_INSPECTION_ASSETS:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Could not inspect production asset {path}: {result.get('error')}",
                    "tool_results": tool_results, "verified": False}
        inspected_assets[path] = result["content"]

    enemy_prefab_path = "UnityProject/Assets/Prefabs/Enemy.prefab"
    if "attackDamage:" not in inspected_assets[enemy_prefab_path]:
        return {"success": False, "task_id": task_id,
                "error": "Enemy prefab does not contain the inspected EnemyController attackDamage setting.",
                "tool_results": tool_results, "verified": False}

    wave_source = inspected["WaveManager.cs"]
    required_api = (
        "startingEnemies", "additionalEnemiesPerWave", "maximumWaves",
        "public int CurrentWave", "public int ActiveEnemyCount", "health.Died += HandleEnemyDied",
    )
    if not all(token in wave_source for token in required_api):
        return {"success": False, "task_id": task_id,
                "error": "WaveManager does not expose the inspected T4.3 wave/death APIs.",
                "tool_results": tool_results, "verified": False}

    test_files = (
        "UnityProject/Assets/Tests/PlayMode/T43/T43MultiWaveValidation.asmdef",
        "UnityProject/Assets/Tests/PlayMode/T43/T43MultiWaveValidationTests.cs",
    )
    sources = {}
    for path in test_files:
        result = _run_tool("read_file", path)
        tool_results.append(result)
        if not result.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Missing isolated PlayMode test file {path}: {result.get('error')}",
                    "tool_results": tool_results, "verified": False}
        sources[path] = result["content"]
    enemy_health_path = "UnityProject/Assets/Scripts/EnemyHealth.cs"
    sources[enemy_health_path] = inspected["EnemyHealth.cs"]
    sources[enemy_prefab_path] = inspected_assets[enemy_prefab_path]

    deployed, verified = [], []
    for path, source in sources.items():
        relative_path = ("Assets/Scripts/EnemyHealth.cs" if path == enemy_health_path
                         else "Assets/Prefabs/Enemy.prefab" if path == enemy_prefab_path
                         else "Assets/Tests/PlayMode/T43/" + Path(path).name)
        result = unity_create_file(relative_path, source)
        tool_results.append({"tool": "unity_create_file", **result})
        if not result.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Test deployment failed for {relative_path}: {result.get('error')}",
                    "files_deployed": deployed, "files_verified": verified,
                    "tool_results": tool_results, "verified": False}
        deployed.append(relative_path)
        exists = TOOLS["unity_file_exists"](relative_path)
        readback = TOOLS["unity_read_file"](relative_path) if exists.get("exists") else None
        if (not exists.get("success") or not exists.get("exists") or readback is None
                or not readback.get("success") or readback.get("content") != source):
            return {"success": False, "task_id": task_id,
                    "error": f"Deployed test read-back failed for {relative_path}",
                    "files_deployed": deployed, "files_verified": verified,
                    "tool_results": tool_results, "verified": False}
        verified.append(relative_path)

    compilation = TOOLS["validate_unity_compilation"]()
    tool_results.append({"tool": "validate_unity_compilation", **compilation})
    if compilation.get("exit_code") != 0 or compilation.get("compiler_errors"):
        print("T4.3 compile diagnostic:")
        print(f"  Unity exit code: {compilation.get('exit_code')}")
        for diagnostic in compilation.get("compiler_errors", []):
            print(f"  C# compiler error: {diagnostic}")
        for message in compilation.get("unity_messages", []):
            print(f"  Unity message: {message}")
        return {"success": False, "task_id": task_id,
                "error": "Unity production and test scripts did not compile cleanly.",
                "files_deployed": deployed, "files_verified": verified,
                "compilation": compilation, "tool_results": tool_results, "verified": False}

    runtime = run_unity_t43_playmode_validation()
    tool_results.append({"tool": "run_unity_t43_playmode_validation", **runtime})
    success = (runtime.get("success", False) and len(verified) == len(sources)
               and compilation.get("exit_code") == 0
               and not compilation.get("compiler_errors"))
    print("T4.3 multiple-wave validation:")
    for check in runtime.get("runtime_checks", []):
        details = f" - {check['details']}" if check.get("details") else ""
        print(f"  {check['check']}: {check['result']}{details}")
    print(f"  Unity compile exit code: {compilation.get('exit_code')}")
    print(f"  C# compiler errors: {len(compilation.get('compiler_errors', []))}")
    print(f"  PlayMode exit code: {runtime.get('exit_code')}")
    print(f"  T4.3 result: {'PASS' if success else 'FAIL'}")
    return {
        "success": success, "task_id": task_id,
        "files_created": [], "files_modified": ["Assets/Scripts/EnemyHealth.cs"],
        "files_deployed": deployed, "files_verified": verified,
        "balance_changes": [],
        "inspected_scripts": list(inspected), "inspected_assets": list(inspected_assets),
        "compilation": compilation, "runtime_validation": runtime,
        "verified": success,
        "error": None if success else "T4.3 compile or PlayMode validation failed.",
        "tool_results": tool_results,
    }


PREPARATION_INSPECTION_SCRIPTS = (
    "PlayerController.cs", "ThirdPersonCamera.cs", "PlayerHealth.cs", "PlayerWeapon.cs",
    "EnemyHealth.cs", "EnemyController.cs", "WaveManager.cs", "ScoreManager.cs",
    "GameHUD.cs", "GameStateManager.cs",
)


def execute_t4_3_preparation(state: AgentState) -> dict[str, Any]:
    """Assemble existing gameplay components in SampleScene and run a minimal smoke check."""
    task_id = "T4.3.PREP"
    tool_results: list[dict[str, Any]] = []
    script_sources: dict[str, str] = {}
    for filename in PREPARATION_INSPECTION_SCRIPTS:
        workspace_path = "UnityProject/Assets/Scripts/" + filename
        readback = _run_tool("read_file", workspace_path)
        tool_results.append(readback)
        if not readback.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Could not inspect {workspace_path}: {readback.get('error')}",
                    "tool_results": tool_results, "verified": False}
        source = readback["content"]
        deployed = TOOLS["unity_read_file"]("Assets/Scripts/" + filename)
        if not deployed.get("success") or deployed.get("content") != source:
            return {"success": False, "task_id": task_id,
                    "error": f"Workspace and Unity production script differ: {filename}",
                    "tool_results": tool_results, "verified": False}
        script_sources[filename] = source

    api_contracts = {
        "PlayerController.cs": ("RequireComponent(typeof(CharacterController))", "cameraTransform", "characterController.Move", "Input.GetAxisRaw"),
        "ThirdPersonCamera.cs": ("private Transform target", "private void LateUpdate()", "Input.GetAxis("),
        "PlayerHealth.cs": ("public float MaxHealth", "public void TakeDamage(float amount)"),
        "PlayerWeapon.cs": ("private Camera aimCamera", "public void Fire()", "public void Reload()"),
        "EnemyHealth.cs": ("public event Action<EnemyHealth> Died", "public void TakeDamage(float amount)"),
        "EnemyController.cs": ("RequireComponent(typeof(EnemyHealth))", "playerTarget", "FindGameObjectWithTag(\"Player\")"),
        "WaveManager.cs": ("private GameObject enemyPrefab", "private Transform[] spawnPoints", "private ScoreManager scoreManager", "public void BeginWaves()"),
        "ScoreManager.cs": ("public int CurrentScore", "public void AwardPoints(int points)"),
        "GameHUD.cs": ("CreateHudAfterSceneLoad", "FindFirstObjectByType<WaveManager>()"),
        "GameStateManager.cs": ("CreateManagerAfterSceneLoad", "BindGameplayReferences()", "waveManager.BeginWaves()"),
    }
    for filename, tokens in api_contracts.items():
        if not all(token in script_sources[filename] for token in tokens):
            return {"success": False, "task_id": task_id,
                    "error": f"Inspected production API contract changed in {filename}; refusing guessed wiring.",
                    "tool_results": tool_results, "verified": False}

    settings_result = TOOLS["unity_read_file"]("ProjectSettings/ProjectSettings.asset")
    if not settings_result.get("success"):
        return {"success": False, "task_id": task_id,
                "error": "Could not inspect Unity's active input handling setting.",
                "tool_results": tool_results, "verified": False}
    input_match = re.search(r"(?m)^\s*activeInputHandler:\s*(\d+)\s*$", settings_result["content"])
    if input_match is None:
        return {"success": False, "task_id": task_id,
                "error": "ProjectSettings.asset does not expose activeInputHandler; refusing to guess.",
                "tool_results": tool_results, "verified": False}
    input_setting_change = {"old_value": int(input_match.group(1)),
                            "new_value": int(input_match.group(1)), "changed": False}
    if input_setting_change["old_value"] == 1:
        input_setting_change = TOOLS["unity_enable_legacy_input_compatibility"]()
        tool_results.append({"tool": "unity_enable_legacy_input_compatibility", **input_setting_change})
        if not input_setting_change.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": "Could not enable legacy Input API compatibility required by the existing scripts.",
                    "tool_results": tool_results, "verified": False}
        settings_result = TOOLS["unity_read_file"]("ProjectSettings/ProjectSettings.asset")
        if not settings_result.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": "Could not read back active input handling after its update.",
                    "tool_results": tool_results, "verified": False}
        staged_settings = _run_tool("create_file", "UnityProject/ProjectSettings/ProjectSettings.asset",
                                    settings_result["content"])
        tool_results.append(staged_settings)
        settings_readback = _run_tool("read_file", "UnityProject/ProjectSettings/ProjectSettings.asset")
        tool_results.append(settings_readback)
        if (not staged_settings.get("success") or not settings_readback.get("success")
                or settings_readback.get("content") != settings_result["content"]):
            return {"success": False, "task_id": task_id,
                    "error": "Workspace ProjectSettings read-back failed.",
                    "tool_results": tool_results, "verified": False}
    elif input_setting_change["old_value"] not in (0, 2):
        return {"success": False, "task_id": task_id,
                "error": f"Unexpected activeInputHandler value {input_setting_change['old_value']}.",
                "tool_results": tool_results, "verified": False}

    deploy_sources = (
        "UnityProject/Assets/Editor/PreparePlayableScene.cs",
        "UnityProject/Assets/Tests/PlayMode/ScenePreparation/ScenePreparationSmoke.asmdef",
        "UnityProject/Assets/Tests/PlayMode/ScenePreparation/ScenePreparationSmokeTests.cs",
        "UnityProject/Assets/Tests/PlayMode/T43/T43MultiWaveValidation.asmdef",
        "UnityProject/Assets/Tests/PlayMode/T43/T43MultiWaveValidationTests.cs",
    )
    sources: dict[str, str] = {}
    for path in deploy_sources:
        readback = _run_tool("read_file", path)
        tool_results.append(readback)
        if not readback.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Could not inspect preparation asset {path}: {readback.get('error')}",
                    "tool_results": tool_results, "verified": False}
        sources[path] = readback["content"]

    # T4.3's test assembly was initially deployed beside T4.2's assembly; Unity rejects
    # multiple asmdefs in one directory. Keep the unfinished T4.3 bridge isolated below PlayMode/T43/.
    legacy_test_files = (
        "Assets/Tests/PlayMode/T43MultiWaveValidation.asmdef",
        "Assets/Tests/PlayMode/T43MultiWaveValidation.asmdef.meta",
        "Assets/Tests/PlayMode/T43MultiWaveValidationTests.cs",
        "Assets/Tests/PlayMode/T43MultiWaveValidationTests.cs.meta",
    )
    deleted_legacy_files = []
    for path in legacy_test_files:
        deletion = unity_delete_file(path)
        tool_results.append({"tool": "unity_delete_file", **deletion})
        if not deletion.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Could not isolate the T4.3 test assembly at {path}: {deletion.get('error')}",
                    "tool_results": tool_results, "verified": False}
        if deletion.get("deleted"):
            deleted_legacy_files.append(path)

    deployed_files, verified_files = [], []
    for workspace_path, source in sources.items():
        relative_path = workspace_path.replace("UnityProject/", "", 1)
        relative_path = relative_path.replace("Assets/Tests/PlayMode/T43/", "Assets/Tests/PlayMode/T43/", 1)
        deployment = unity_create_file(relative_path, source)
        tool_results.append({"tool": "unity_create_file", **deployment})
        if not deployment.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Deployment failed for {relative_path}: {deployment.get('error')}",
                    "files_deployed": deployed_files, "files_verified": verified_files,
                    "tool_results": tool_results, "verified": False}
        deployed_files.append(relative_path)
        deployed_readback = TOOLS["unity_read_file"](relative_path)
        if not deployed_readback.get("success") or deployed_readback.get("content") != source:
            return {"success": False, "task_id": task_id,
                    "error": f"Deployed source verification failed for {relative_path}",
                    "files_deployed": deployed_files, "files_verified": verified_files,
                    "tool_results": tool_results, "verified": False}
        verified_files.append(relative_path)

    editor_result = run_unity_editor_method(
        "ScenePreparation.PreparePlayableScene.Run",
        "[SCENE PREP] Scene objects, references, prefab, and build entry: PASS")
    tool_results.append({"tool": "run_unity_editor_method", **editor_result})
    if not editor_result.get("success"):
        print(f"SampleScene Editor assembly exit code: {editor_result.get('exit_code')}")
        for diagnostic in editor_result.get("compiler_errors", []):
            print(f"  C# compiler error: {diagnostic}")
        for message in editor_result.get("unity_messages", []):
            print(f"  Unity message: {message}")
        log_tail = editor_result.get("unity_log", "")
        if log_tail:
            print("  Unity log tail:")
            print(log_tail[-5000:])
        return {"success": False, "task_id": task_id,
                "error": "Unity could not assemble SampleScene. See the Editor method diagnostics.",
                "files_deployed": deployed_files, "files_verified": verified_files,
                "deleted_legacy_test_files": deleted_legacy_files,
                "editor_assembly": editor_result, "tool_results": tool_results, "verified": False}

    generated_assets = (
        ("Assets/Scenes/SampleScene.unity", "UnityProject/Assets/Scenes/SampleScene.unity"),
        ("Assets/Scenes/SampleScene.unity.meta", "UnityProject/Assets/Scenes/SampleScene.unity.meta"),
        ("Assets/Prefabs.meta", "UnityProject/Assets/Prefabs.meta"),
        ("Assets/Prefabs/Enemy.prefab", "UnityProject/Assets/Prefabs/Enemy.prefab"),
        ("Assets/Prefabs/Enemy.prefab.meta", "UnityProject/Assets/Prefabs/Enemy.prefab.meta"),
    )
    generated_verified = []
    for project_path, workspace_path in generated_assets:
        asset = TOOLS["unity_read_file"](project_path)
        if not asset.get("success"):
            return {"success": False, "task_id": task_id,
                    "error": f"Unity-generated asset could not be read back: {project_path}",
                    "files_deployed": deployed_files, "files_verified": verified_files,
                    "generated_assets_verified": generated_verified,
                    "tool_results": tool_results, "verified": False}
        staged = _run_tool("create_file", workspace_path, asset["content"])
        tool_results.append(staged)
        confirmed = _run_tool("read_file", workspace_path) if staged.get("success") else None
        tool_results.extend([confirmed] if confirmed else [])
        if not confirmed or not confirmed.get("success") or confirmed.get("content") != asset["content"]:
            return {"success": False, "task_id": task_id,
                    "error": f"Workspace read-back failed for generated asset {workspace_path}",
                    "files_deployed": deployed_files, "files_verified": verified_files,
                    "generated_assets_verified": generated_verified,
                    "tool_results": tool_results, "verified": False}
        generated_verified.append(project_path)

    compilation = TOOLS["validate_unity_compilation"]()
    tool_results.append({"tool": "validate_unity_compilation", **compilation})
    if compilation.get("exit_code") != 0 or compilation.get("compiler_errors"):
        print(f"Unity compile exit code: {compilation.get('exit_code')}")
        for diagnostic in compilation.get("compiler_errors", []):
            print(f"  C# compiler error: {diagnostic}")
        for message in compilation.get("unity_messages", []):
            print(f"  Unity message: {message}")
        return {"success": False, "task_id": task_id,
                "error": "Unity compilation failed after scene assembly.",
                "files_deployed": deployed_files, "files_verified": verified_files,
                "generated_assets_verified": generated_verified,
                "compilation": compilation, "tool_results": tool_results, "verified": False}

    smoke = run_unity_scene_preparation_smoke()
    tool_results.append({"tool": "run_unity_scene_preparation_smoke", **smoke})
    success = smoke.get("success", False) and len(generated_verified) == len(generated_assets)
    print("SampleScene preparation report:")
    for check in smoke.get("runtime_checks", []):
        detail = " - " + check["details"] if check.get("details") else ""
        print(f"  {check['check']}: {check['result']}{detail}")
    print(f"  Unity compile exit code: {compilation.get('exit_code')}")
    print(f"  PlayMode smoke exit code: {smoke.get('exit_code')}")
    for failure in smoke.get("test_failures", []):
        print(f"  Smoke test failure: {failure}")
    for message in smoke.get("unity_messages", []):
        print(f"  Unity message: {message}")
    print(f"  Preparation complete: {'YES' if success else 'NO'}")
    return {"success": success, "task_id": task_id,
        "files_created": ["Assets/Prefabs/Enemy.prefab", "Assets/Scenes/SampleScene.unity"],
        "files_modified": ["Assets/Scenes/SampleScene.unity"]
            + (["ProjectSettings/ProjectSettings.asset"] if input_setting_change.get("changed") else []),
        "files_deployed": deployed_files + generated_verified,
        "files_verified": verified_files + generated_verified,
        "deleted_legacy_test_files": deleted_legacy_files,
        "input_setting_change": input_setting_change,
        "compilation": compilation, "smoke_test": smoke,
        "verified": success,
        "error": None if success else "Scene assembly, compilation, or smoke validation failed.",
        "tool_results": tool_results}


TASK_EXECUTORS = {
    "T1.1": execute_t1_1,
    "T1.2": execute_t1_2,
    "T1.3": execute_t1_3,
    "T2.1": execute_t2_1,
    "T2.2": execute_t2_2,
    "T2.3": execute_t2_3,
    "T3.1": execute_t3_1,
    "T3.2": execute_t3_2,
    "T3.3": execute_t3_3,
    "T4.1": execute_t4_1,
    "T4.2": execute_t4_2,
    "T4.3.PREP": execute_t4_3_preparation,
    "T4.3": execute_t4_3,
    "PHASE2.RECOVERY": lambda state: {
        "success": True,
        "error": None,
        "tool_results": [],
        "validation_request": {
            "fixture_path": "Assets/Tests/RecoveryFixture/RecoveryProbe.cs",
            "files": {"Assets/Tests/RecoveryFixture/RecoveryProbe.cs":
                      "public static class RecoveryProbe { public static int Value = RECOVERY_FIXTURE_MISSING_SYMBOL; }\n"},
            "timeout_seconds": 600,
        },
    },
}


def load_plan(state: AgentState) -> dict:
    # Nodes transform state; this node keeps the plan and initializes completion data.
    return {
        "plan": state["plan"],
        "completed_tasks": state.get("completed_tasks", []),
        "available_tasks": state.get("available_tasks", []),
        "current_task": state.get("current_task"),
        "execution_results": state.get("execution_results", {}),
        "tool_results": state.get("tool_results", []),
        "failed_tasks": state.get("failed_tasks", []),
        "execution_error": state.get("execution_error"),
        "task_execution": state.get("task_execution"),
        "validation_request": state.get("validation_request"),
        "validation_result": state.get("validation_result"),
        "failure_diagnosis": state.get("failure_diagnosis"),
        "diagnosis_attempt": state.get("diagnosis_attempt"),
        "repair_attempts": state.get("repair_attempts", 0),
        "max_repair_attempts": state.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS),
        "validation_history": state.get("validation_history", []),
        "repair_action": state.get("repair_action"),
        "task_runs": state.get("task_runs", []),
    }


def find_available_tasks(state: AgentState) -> dict:
    # Reuse the deterministic resolver so the LLM does not choose runnable tasks.
    tasks = get_available_tasks(state["plan"], state["completed_tasks"])
    available_task_ids = [task.id for task in tasks]
    if not available_task_ids:
        all_task_ids = set(get_all_tasks(state["plan"]))
        completed = set(state["completed_tasks"])
        if not all_task_ids.issubset(completed):
            remaining = sorted(all_task_ids - completed)
            print(
                "Warning: no tasks are available, but tasks remain incomplete. "
                "There may be a circular or unresolved dependency. "
                f"Remaining tasks: {remaining}"
            )
    return {"available_tasks": available_task_ids}


def select_task(state: AgentState) -> dict:
    # This node selects a task ID only; it does not execute the task.
    available = state["available_tasks"]
    return {"current_task": available[0] if available else None}


def execute_task(state: AgentState) -> dict:
    """Dispatch the selected task to its registered, fixed executor."""
    task_id = state["current_task"]
    if task_id is None:
        return {}

    task = get_all_tasks(state["plan"]).get(task_id)
    if task is None:
        execution = {"success": False, "error": "Selected task ID is not in the plan", "tool_results": []}
    elif task_id in TASK_EXECUTORS:
        print("\n========== TASK EXECUTION ==========")
        print(f"\nTask: {task.id}")
        print(f"Description: {task.description}")
        execution = TASK_EXECUTORS[task_id](state)
    elif task.capability_id:
        print("\n========== CAPABILITY EXECUTION ==========")
        print(f"\nTask: {task.id} ({task.capability_id})")
        print(f"Description: {task.description}")
        executor = CAPABILITY_TASK_EXECUTORS.get(task.capability_id)
        if executor is None:
            execution = {
                "success": False,
                "task_id": task.id,
                "error": (
                    f"No bounded executor is registered for capability "
                    f"'{task.capability_id}'."
                ),
                "tool_results": [],
                "verified": False,
            }
        else:
            execution = executor(state, task)
    else:
        execution = {
            "success": False,
            "error": f"No executor implemented for task '{task_id}' and it declares no capability.",
            "tool_results": [],
        }

    task_tool_results = execution.get("tool_results", [])

    all_tool_results = list(state["tool_results"]) + task_tool_results
    return {"task_execution": execution, "tool_results": all_tool_results,
            "validation_request": execution.get("validation_request"),
            "repair_attempts": 0, "validation_result": None,
            "failure_diagnosis": None, "diagnosis_attempt": None,
            "repair_action": None, "validation_history": []}


def _validation_from_execution(state: AgentState) -> dict[str, Any]:
    """Normalize fixed task executor evidence into the common result model."""
    execution = state.get("task_execution") or {}
    tools = execution.get("tool_results", [])
    failures: list[dict[str, Any]] = []
    compiler_errors: list[dict[str, Any]] = []
    runtime_errors: list[str] = []
    stdout_parts, stderr_parts, log_parts = [], [], []
    exit_code = None
    files_changed = list(execution.get("files_deployed", execution.get("files_changed", [])))
    for item in tools:
        sources = [item] if isinstance(item, dict) else []
        for key in ("compilation", "smoke_test", "runtime_validation", "playmode", "validation"):
            result = item.get(key) if isinstance(item, dict) else None
            if not isinstance(result, dict):
                continue
            sources.append(result)
        for result in sources:
            compiler_errors.extend(result.get("compiler_errors", []))
            runtime_errors.extend(result.get("runtime_errors", []))
            failures.extend(result.get("test_failures", []))
            exit_code = result.get("exit_code", exit_code)
            stdout_parts.append(result.get("stdout", ""))
            stderr_parts.append(result.get("stderr", ""))
            log_parts.append(result.get("unity_log", ""))
    success = bool(execution.get("success"))
    if success:
        failure_type = "success"
        summary = "Task executor compilation and runtime validation passed."
    elif compiler_errors:
        failure_type, summary = "compile_failed", "Task executor reported Unity compilation errors."
    elif failures or runtime_errors:
        failure_type, summary = "runtime_test_failed", "Task executor runtime validation failed."
    else:
        failure_type, summary = "unknown_error", execution.get("error", "Task execution failed without structured diagnostics.")
    return ValidationResult(
        task_id=state.get("current_task") or "unknown", success=success,
        failure_type=failure_type, summary=summary, compiler_errors=compiler_errors,
        runtime_errors=runtime_errors, test_failures=failures,
        stdout="\n".join(filter(None, stdout_parts))[-16000:],
        stderr="\n".join(filter(None, stderr_parts))[-16000:],
        unity_log="\n".join(filter(None, log_parts))[-16000:],
        exit_code=exit_code, files_changed=files_changed,
        attempt_number=len(state.get("validation_history", [])) + 1,
    ).model_dump()


def validate_task(state: AgentState) -> dict:
    request = state.get("validation_request")
    if request:
        result = TOOLS["validate_unity_files"](
            request["files"], task_id=state.get("current_task") or "unknown",
            playmode_assembly=request.get("playmode_assembly"),
            attempt_number=len(state.get("validation_history", [])) + 1,
            timeout_seconds=request.get("timeout_seconds", 600),
        )
        result = ValidationResult.model_validate(result).model_dump()
    else:
        result = _validation_from_execution(state)
    history = list(state.get("validation_history", [])) + [result]
    print(f"Attempt {result['attempt_number']}: VALIDATION {result['failure_type']} — {result['summary']}")
    return {"validation_result": result, "validation_history": history}


def route_validation(state: AgentState) -> str:
    return "success" if (state.get("validation_result") or {}).get("success") else "failure"


def _generate_gemini_structured(contents: str, schema: type[BaseModel]):
    """Use the existing Gemini client with the same bounded transient retries as planning."""
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception as exc:
        raise GeminiInitializationError() from exc

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(
                model=MODEL, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_schema=schema),
            )
        except Exception as exc:
            if _is_transient_gemini_error(exc) and attempt < MAX_ATTEMPTS:
                print(f"Gemini temporarily unavailable during structured recovery (attempt {attempt}); retrying.")
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise


class GeminiInitializationError(RuntimeError):
    """Indicates that the Gemini client could not be initialized."""

    def __init__(self) -> None:
        super().__init__("Gemini client initialization failed.")


def _is_transient_gemini_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, errors.APIError)):
        code = getattr(exc, "code", None)
        try:
            if int(code) in TEMPORARY_ERROR_CODES:
                return True
        except (TypeError, ValueError):
            pass

    exception_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "timeout" in exception_name
        or "deadlineexceeded" in exception_name
        or "unavailable" in exception_name
        or "timeout" in message
        or "deadline exceeded" in message
        or "temporarily unavailable" in message
        or "service unavailable" in message
        or "unavailable" in message
        or re.search(
            r"(?:http(?: status)?\s*|status(?:_code)?[=: ]+|error\s+)"
            r"(408|429|500|502|503|504)\b",
            message,
        ) is not None
    )


def _safe_gemini_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", message)
    message = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    return message[:500]


def _asset_path_from_evidence(path: str, known_files: list[str]) -> str | None:
    normalized = path.strip().replace("\\", "/")
    match = re.search(r"(?:^|[/\s])(Assets/.+?\.cs)(?:$|[:(])", normalized, re.IGNORECASE)
    if not match:
        return None
    found = match.group(1)
    for known_path in known_files:
        known_normalized = known_path.replace("\\", "/")
        if known_normalized.casefold() == found.casefold():
            return known_path
    return found


def _deterministic_failure_diagnosis(
    result: ValidationResult,
    request: dict[str, Any] | None,
    llm_error: str,
) -> FailureDiagnosis:
    """Build a conservative diagnosis solely from structured validation evidence."""
    if result.failure_type == "compile_failed":
        for compiler_error in result.compiler_errors:
            code = str(compiler_error.get("code", "")).strip()
            message = str(compiler_error.get("message", "")).strip()
            compiler_file = str(compiler_error.get("file", "")).strip()
            relevant_path = _asset_path_from_evidence(compiler_file, result.files_changed)
            if not (re.fullmatch(r"CS\d{4}", code) and message and relevant_path):
                continue

            fixture_path = (request or {}).get("fixture_path")
            fixture_source = ((request or {}).get("files") or {}).get(fixture_path, "")
            marker = "RECOVERY_FIXTURE_MISSING_SYMBOL"
            only_fixture_diagnostic = bool(result.compiler_errors) and all(
                isinstance(item, dict)
                and str(item.get("code", "")).strip() == code
                and str(item.get("message", "")).strip() == message
                and _asset_path_from_evidence(
                    str(item.get("file", "")), result.files_changed
                ) == relevant_path
                for item in result.compiler_errors
            )
            is_controlled_fixture_failure = (
                relevant_path == fixture_path
                and code == "CS0103"
                and marker in message
                and marker in fixture_source
                and only_fixture_diagnostic
            )
            repair_strategy = (
                "Replace only the controlled fixture's unresolved marker with its fixed literal; "
                "the compiler diagnostic identifies CS0103 for that exact marker."
                if is_controlled_fixture_failure
                else "The compiler diagnostic identifies the failing source and message, but no "
                     "deterministic source change is authorized; stop without modifying files."
            )
            return FailureDiagnosis(
                failure_type="compile_failed",
                root_cause=f"Unity compiler reported {code} in {relevant_path}: {message}",
                relevant_files=[relevant_path],
                repair_required=True,
                automatically_fixable=is_controlled_fixture_failure,
                repair_strategy=repair_strategy,
                evidence_quotes=[message],
                diagnosis_source="deterministic_fallback",
                llm_error=llm_error,
                error_code=code,
                compiler_message=message,
            )

        return FailureDiagnosis(
            failure_type="compile_failed",
            root_cause="Unable to determine a safe automatic repair from available evidence.",
            relevant_files=[],
            repair_required=False,
            automatically_fixable=False,
            repair_strategy="Stop without modifying files; no complete, path-bound compiler diagnostic is available.",
            evidence_quotes=[],
            uncertainty="Structured compiler evidence was missing or could not be safely interpreted.",
            diagnosis_source="deterministic_fallback",
            llm_error=llm_error,
        )

    if result.failure_type == "runtime_test_failed":
        failures = [failure for failure in result.test_failures if isinstance(failure, dict)]
        if failures:
            failure = failures[0]
            test_name = str(failure.get("name", "")).strip() or "unknown test"
            message = str(failure.get("message", "")).strip()
            stack_trace = str(failure.get("stack_trace", "")).strip()
            exception = re.search(
                r"(?m)^([\w.+`<>]+(?:Exception|Error)):\s*(.*)$",
                message + ("\n" + stack_trace if stack_trace else ""),
            )
            exception_type = exception.group(1) if exception else None
            exception_message = exception.group(2).strip() if exception else (message or None)
            stack_paths = re.findall(r"([^\r\n()]*?\.cs)(?:\((\d+)(?:,\d+)?\)|:line\s+\d+)", stack_trace)
            relevant_files = []
            for path, _ in stack_paths:
                asset_path = _asset_path_from_evidence(path, result.files_changed)
                if asset_path and asset_path not in relevant_files:
                    relevant_files.append(asset_path)
            evidence_quotes = [quote for quote in (test_name, message, stack_trace) if quote]
            root_cause = f"PlayMode test '{test_name}' failed"
            if exception_type:
                root_cause += f" with {exception_type}"
                if exception_message:
                    root_cause += f": {exception_message}"
            elif message:
                root_cause += f": {message}"
            return FailureDiagnosis(
                failure_type="runtime_test_failed",
                root_cause=root_cause,
                relevant_files=relevant_files,
                repair_required=False,
                automatically_fixable=False,
                repair_strategy="Stop without modifying files; the failing test evidence does not establish a safe automatic repair.",
                evidence_quotes=evidence_quotes,
                diagnosis_source="deterministic_fallback",
                llm_error=llm_error,
                failing_test=test_name,
                exception_type=exception_type,
                exception_message=exception_message,
                stack_trace=stack_trace or None,
            )

        return FailureDiagnosis(
            failure_type="runtime_test_failed",
            root_cause="Unable to determine a safe automatic repair from available evidence.",
            relevant_files=[],
            repair_required=False,
            automatically_fixable=False,
            repair_strategy="Stop without modifying files; no structured test failure was reported.",
            evidence_quotes=[],
            uncertainty="Structured runtime test failure details were not available.",
            diagnosis_source="deterministic_fallback",
            llm_error=llm_error,
        )

    if result.failure_type == "deployment_failed":
        deployment_error = result.summary.strip()
        path_match = re.search(r"Deployment(?: read-back)? failed for ([^:.\s]+\.cs)", deployment_error)
        relevant_path = (
            _asset_path_from_evidence(path_match.group(1), result.files_changed)
            if path_match else None
        )
        return FailureDiagnosis(
            failure_type="deployment_failed",
            root_cause=f"Unity deployment failed: {deployment_error}",
            relevant_files=[relevant_path] if relevant_path else [],
            repair_required=False,
            automatically_fixable=False,
            repair_strategy="Stop without modifying files; the reported deployment error does not establish a safe automatic repair.",
            evidence_quotes=[deployment_error] if deployment_error else [],
            diagnosis_source="deterministic_fallback",
            llm_error=llm_error,
        )

    return FailureDiagnosis(
        failure_type=result.failure_type,
        root_cause="Unable to determine a safe automatic repair from available evidence.",
        relevant_files=[],
        repair_required=False,
        automatically_fixable=False,
        repair_strategy="Stop without modifying files; this failure type has no deterministic recovery rule.",
        evidence_quotes=[],
        uncertainty="No deterministic diagnosis rule exists for this failure type.",
        diagnosis_source="deterministic_fallback",
        llm_error=llm_error,
    )


def diagnose_failure(state: AgentState) -> dict:
    """Prefer Gemini; use deterministic evidence-only diagnosis for availability failures."""
    result = ValidationResult.model_validate(state["validation_result"])
    if not os.getenv("GEMINI_API_KEY"):
        llm_error = "Gemini unavailable: GEMINI_API_KEY is not configured."
        diagnosis = _deterministic_failure_diagnosis(
            result, state.get("validation_request"), llm_error
        )
        return {
            "failure_diagnosis": diagnosis.model_dump(),
            "diagnosis_attempt": {
                "diagnosis_source": diagnosis.diagnosis_source,
                "llm_error": llm_error,
            },
        }

    source_evidence = []
    for path in result.files_changed:
        if path.startswith("Assets/") and path.endswith(".cs"):
            readback = TOOLS["unity_read_file"](path)
            if readback.get("success"):
                source_evidence.append({"path": path, "content": readback.get("content", "")[:12000]})
    prompt = ("Diagnose this Unity validation failure using only the supplied evidence. "
              "Do not invent diagnostics. Cite exact substrings from diagnostics/source in evidence_quotes. "
              "Relevant files must be among supplied source paths. Do not propose weakening tests.\n" +
              json.dumps({"validation": result.model_dump(), "sources": source_evidence}, ensure_ascii=False))
    try:
        response = _generate_gemini_structured(prompt, FailureDiagnosis)
        diagnosis = FailureDiagnosis.model_validate_json(response.text or "{}")
        allowed = {item["path"] for item in source_evidence}
        evidence_text = "\n".join([result.summary, result.stdout, result.stderr, result.unity_log,
                                    json.dumps(result.compiler_errors), json.dumps(result.test_failures)] +
                                   [item["content"] for item in source_evidence])
        if diagnosis.failure_type != result.failure_type or any(path not in allowed for path in diagnosis.relevant_files) \
                or any(not quote or quote not in evidence_text for quote in diagnosis.evidence_quotes):
            raise ValueError("Diagnosis did not match the observed failure or cite verifiable evidence.")
        diagnosis = diagnosis.model_copy(update={
            "diagnosis_source": "gemini",
            "llm_error": None,
        })
    except Exception as exc:
        if isinstance(exc, GeminiInitializationError) or _is_transient_gemini_error(exc):
            llm_error = _safe_gemini_error(exc)
            diagnosis = _deterministic_failure_diagnosis(
                result, state.get("validation_request"), llm_error
            )
        else:
            llm_error = _safe_gemini_error(exc)
            diagnosis = FailureDiagnosis(
                failure_type=result.failure_type,
                root_cause="Structured Gemini diagnosis failed validation.",
                relevant_files=[],
                repair_required=False,
                automatically_fixable=False,
                repair_strategy="Stop without changing files; a non-availability Gemini diagnosis error is not eligible for fallback.",
                evidence_quotes=[],
                uncertainty=llm_error,
                diagnosis_source="gemini",
                llm_error=llm_error,
            )
    print(f"Diagnosis: {diagnosis.root_cause}; repair={diagnosis.repair_required}; "
          f"automatic={diagnosis.automatically_fixable}; source={diagnosis.diagnosis_source}")
    return {
        "failure_diagnosis": diagnosis.model_dump(),
        "diagnosis_attempt": {
            "diagnosis_source": diagnosis.diagnosis_source,
            "llm_error": diagnosis.llm_error,
        },
    }


def repair_task(state: AgentState) -> dict:
    """Apply a narrowly constrained repair; the recovery fixture has a fixed safe patch."""
    diagnosis = FailureDiagnosis.model_validate(state.get("failure_diagnosis") or {})
    validation = ValidationResult.model_validate(state.get("validation_result") or {})
    evidence_text = "\n".join((
        validation.summary,
        validation.stdout,
        validation.stderr,
        validation.unity_log,
        json.dumps(validation.compiler_errors),
        json.dumps(validation.test_failures),
    ))
    for path in diagnosis.relevant_files:
        if path.startswith("Assets/") and path.endswith(".cs"):
            readback = TOOLS["unity_read_file"](path)
            if readback.get("success"):
                evidence_text += "\n" + readback.get("content", "")
    if (state.get("repair_attempts", 0) >= state.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS)
            or not diagnosis.repair_required or not diagnosis.automatically_fixable
            or diagnosis.uncertainty or not diagnosis.evidence_quotes
            or any(quote not in evidence_text for quote in diagnosis.evidence_quotes)):
        return {"repair_action": {"success": False, "summary": "Repair stopped by bounded/safety checks."}}
    request = state.get("validation_request") or {}
    fixture_path = request.get("fixture_path")
    if fixture_path and fixture_path in diagnosis.relevant_files:
        result = state["validation_result"]
        source = request.get("files", {}).get(fixture_path, "")
        marker = "RECOVERY_FIXTURE_MISSING_SYMBOL"
        compiler_errors = result.get("compiler_errors", [])
        matching_fixture_errors = bool(compiler_errors) and all(
            isinstance(error, dict)
            and error.get("code") == "CS0103"
            and _asset_path_from_evidence(
                str(error.get("file", "")), result.get("files_changed", [])
            ) == fixture_path
            and marker in str(error.get("message", ""))
            for error in compiler_errors
        )
        if (diagnosis.error_code not in (None, "CS0103")
                or not matching_fixture_errors or marker not in source
                or not any(marker in quote for quote in diagnosis.evidence_quotes)):
            return {"repair_action": {"success": False, "summary": "Fixture evidence did not match the expected compiler diagnostic."}}
        request["files"][fixture_path] = source.replace(marker, "1", 1)
        return {"validation_request": request, "repair_attempts": state.get("repair_attempts", 0) + 1,
                "repair_action": {"success": True, "file": fixture_path,
                                  "summary": "Replaced the fixture's unresolved symbol after matching CS0103 evidence."}}
    # General production/test repairs require a Gemini structured exact patch.
    result = ValidationResult.model_validate(state["validation_result"])
    candidates = set(result.files_changed) & set(diagnosis.relevant_files)
    candidates = {path for path in candidates if path.startswith("Assets/") and path.endswith(".cs")}
    if not candidates or not os.getenv("GEMINI_API_KEY"):
        return {"repair_action": {"success": False,
                                  "summary": "No verified, in-scope repair proposal is available."}}
    source_map = {}
    for path in candidates:
        response = TOOLS["unity_read_file"](path)
        if response.get("success"):
            source_map[path] = response.get("content", "")
    if not source_map:
        return {"repair_action": {"success": False, "summary": "Could not read the proposed repair source."}}
    prompt = ("Propose one minimal exact text replacement to repair the observed Unity failure. "
              "Use only these files and cite exact evidence. Never remove/disable tests or assertions. "
              "If uncertain, propose no-op original_text == replacement_text.\n" +
              json.dumps({"diagnosis": diagnosis.model_dump(), "validation": result.model_dump(),
                          "sources": source_map}, ensure_ascii=False))
    try:
        response = _generate_gemini_structured(prompt, RepairProposal)
        proposal = RepairProposal.model_validate_json(response.text or "{}")
        current = source_map.get(proposal.file_path)
        if (current is None or not proposal.original_text or proposal.original_text == proposal.replacement_text
                or current.count(proposal.original_text) != 1
                or any(q not in (result.summary + result.stdout + result.stderr + result.unity_log
                                 + json.dumps(result.compiler_errors) + json.dumps(result.test_failures)
                                 + current) for q in proposal.evidence_quotes)):
            raise ValueError("Patch is not unique, in scope, evidence-backed, or changes no text.")
        before_markers = sum(current.count(marker) for marker in ("Assert.", "[Test]", "[UnityTest]"))
        repaired = current.replace(proposal.original_text, proposal.replacement_text, 1)
        after_markers = sum(repaired.count(marker) for marker in ("Assert.", "[Test]", "[UnityTest]"))
        if after_markers < before_markers:
            raise ValueError("Patch would remove test/assertion markers.")
        deployed = TOOLS["unity_create_file"](proposal.file_path, repaired)
        if not deployed.get("success"):
            raise ValueError(deployed.get("error", "Unity deployment failed."))
        readback = TOOLS["unity_read_file"](proposal.file_path)
        if not readback.get("success") or readback.get("content") != repaired:
            raise ValueError("Repair deployment did not pass read-back verification.")
        if request and proposal.file_path in request.get("files", {}):
            request["files"][proposal.file_path] = repaired
        return {"validation_request": request or None,
                "repair_attempts": state.get("repair_attempts", 0) + 1,
                "repair_action": {"success": True, "file": proposal.file_path,
                                  "summary": proposal.reason}}
    except Exception as exc:
        return {"repair_action": {"success": False,
                                  "summary": f"Repair proposal rejected: {str(exc)[:300]}"}}


def route_repair(state: AgentState) -> str:
    action = state.get("repair_action") or {}
    if action.get("success") and state.get("repair_attempts", 0) <= state.get("max_repair_attempts", MAX_REPAIR_ATTEMPTS):
        return "retry"
    return "stop"


def cleanup_validation_fixture(state: AgentState) -> dict:
    request = state.get("validation_request") or {}
    removed = []
    errors = []
    for path in [request.get("fixture_path"), request.get("fixture_path") + ".meta" if request.get("fixture_path") else None]:
        if path:
            response = TOOLS["unity_delete_file"](path)
            (removed if response.get("success") else errors).append(path)
    if request.get("workspace_fixture"):
        response = TOOLS["delete_file"](request["workspace_fixture"])
        if not response.get("success"):
            errors.append(request["workspace_fixture"])
    print(f"Fixture cleanup: removed={removed}; errors={errors}")
    return {"repair_action": {**(state.get("repair_action") or {}), "cleanup_errors": errors,
                              "fixture_removed": not errors}}


def mark_task_failed(state: AgentState) -> dict:
    task_id = state.get("current_task")
    results = dict(state.get("execution_results", {}))
    failed = list(state.get("failed_tasks", []))
    if task_id:
        results[task_id] = "failed"
        if task_id not in failed:
            failed.append(task_id)
    result = state.get("validation_result") or {}
    error = result.get("summary") or (state.get("repair_action") or {}).get("summary") or "Task validation failed."
    print(f"FINAL: FAILED; attempts={len(state.get('validation_history', []))}; files={result.get('files_changed', [])}")
    runs = list(state.get("task_runs", []))
    if task_id:
        task = get_all_tasks(state["plan"]).get(task_id)
        runs.append({
            "task_id": task_id,
            "capability_id": task.capability_id if task else None,
            "execution": state.get("task_execution"),
            "validation_history": state.get("validation_history", []),
            "failure_diagnosis": state.get("failure_diagnosis"),
            "result": "failed",
        })
    return {"execution_results": results, "failed_tasks": failed, "execution_error": error,
            "current_task": None, "task_runs": runs}


def mark_task_complete(state: AgentState) -> dict:
    """Record a successful task once and clear it so the next can be selected."""
    task_id = state["current_task"]
    completed = list(state["completed_tasks"])
    validation_ok = bool((state.get("validation_result") or {}).get("success"))
    cleanup_ok = bool((state.get("repair_action") or {}).get("fixture_removed", True))
    results = dict(state.get("execution_results", {}))
    if task_id and validation_ok and cleanup_ok:
        results[task_id] = "success"
    if task_id and results.get(task_id) == "success" and task_id not in completed:
        completed.append(task_id)
        print("\n========== TASK COMPLETED ==========")
        print(f"\nCompleted: {task_id}")
        print(f"FINAL: SUCCESS; attempts={len(state.get('validation_history', []))}; files={(state.get('validation_result') or {}).get('files_changed', [])}")
    runs = list(state.get("task_runs", []))
    if task_id and validation_ok and cleanup_ok:
        runs.append({
            "task_id": task_id,
            "capability_id": (
                get_all_tasks(state["plan"]).get(task_id).capability_id
                if get_all_tasks(state["plan"]).get(task_id) else None
            ),
            "execution": state.get("task_execution"),
            "validation_history": state.get("validation_history", []),
            "result": "success",
        })
    return {"execution_results": results, "completed_tasks": completed,
            "current_task": None, "task_runs": runs}


def route_execution_result(state: AgentState) -> str:
    """Only successful tool execution is allowed to mark a task complete."""
    return "execute"


def route_after_task_check(state: AgentState) -> str:
    # Conditional routing ends on completion or a dependency deadlock.
    if state["available_tasks"]:
        return "select_task"
    return "end"


def build_agent_graph():
    # StateGraph connects nodes and conditional routes into a repeating task loop.
    graph = StateGraph(AgentState)
    graph.add_node("load_plan", load_plan)
    graph.add_node("find_available_tasks", find_available_tasks)
    graph.add_node("select_task", select_task)
    graph.add_node("execute_task", execute_task)
    graph.add_node("validate_task", validate_task)
    graph.add_node("diagnose_failure", diagnose_failure)
    graph.add_node("repair_task", repair_task)
    graph.add_node("cleanup_validation_fixture", cleanup_validation_fixture)
    graph.add_node("mark_task_complete", mark_task_complete)
    graph.add_node("mark_task_failed", mark_task_failed)
    graph.add_edge(START, "load_plan")
    graph.add_edge("load_plan", "find_available_tasks")
    # Conditional edges choose between continuing the loop and ending safely.
    graph.add_conditional_edges(
        "find_available_tasks",
        route_after_task_check,
        {"select_task": "select_task", "end": END},
    )
    graph.add_edge("select_task", "execute_task")
    graph.add_edge("execute_task", "validate_task")
    graph.add_conditional_edges("validate_task", route_validation,
                                {"success": "cleanup_validation_fixture", "failure": "diagnose_failure"})
    graph.add_edge("diagnose_failure", "repair_task")
    graph.add_conditional_edges("repair_task", route_repair,
                                {"retry": "validate_task", "stop": "cleanup_validation_fixture"})
    graph.add_conditional_edges(
        "cleanup_validation_fixture",
        lambda state: "failed" if (state.get("repair_action") or {}).get("cleanup_errors")
        or not (state.get("validation_result") or {}).get("success") else "success",
        {"success": "mark_task_complete", "failed": "mark_task_failed"},
    )
    graph.add_edge("mark_task_failed", END)
    # Loop back to recompute tasks using the deterministic dependency resolver.
    graph.add_edge("mark_task_complete", "find_available_tasks")
    return graph.compile()


def run_capability_game_build(game_idea: str) -> dict[str, Any]:
    """Plan from the supplied idea and execute its capability tasks through LangGraph."""
    plan = ProjectPlan.model_validate(build_capability_plan(game_idea))
    initial_state: AgentState = {
        "plan": plan,
        "completed_tasks": [],
        "available_tasks": [],
        "current_task": None,
        "execution_results": {},
        "tool_results": [],
        "failed_tasks": [],
        "execution_error": None,
        "task_runs": [],
    }
    print("Capability plan generated from the supplied game idea:")
    print(f"  Project: {plan.project_name}")
    print(f"  Capabilities: {', '.join(plan.selected_capabilities)}")
    result = build_agent_graph().invoke(initial_state)
    print("LangGraph capability execution:")
    for task_run in result.get("task_runs", []):
        print(f"  {task_run['task_id']} ({task_run.get('capability_id')}): {task_run['result']}")
    print(f"  Completed tasks: {len(result.get('completed_tasks', []))}")
    print(f"  Failed tasks: {', '.join(result.get('failed_tasks', [])) or 'none'}")
    return result


def run_t4_3_preparation_task_graph() -> dict[str, Any]:
    """Run only the registered preparation task through the existing LangGraph nodes."""
    plan = ProjectPlan(
        project_name="Unity Agent scene preparation",
        requirements=[],
        phases=[Phase(id=4, name="T4.3 preparation", tasks=[
            Task(id="T4.2", description="Previously verified game flow prerequisite", dependencies=[]),
            Task(id="T4.3.PREP", description="Assemble and smoke-test the existing gameplay scene",
                 dependencies=["T4.2"]),
        ])],
        dependencies=[Dependency(task="T4.3.PREP", depends_on=["T4.2"])],
        assumptions=[],
    )
    initial_state: AgentState = {
        "plan": plan,
        "completed_tasks": ["T4.2"],
        "available_tasks": [],
        "current_task": None,
        "execution_results": {},
        "tool_results": [],
        "failed_tasks": [],
        "execution_error": None,
    }
    result = build_agent_graph().invoke(initial_state)
    print("LangGraph preparation result:")
    print(f"  T4.3.PREP: {result['execution_results'].get('T4.3.PREP', 'not run')}")
    print(f"  Completed tasks: {', '.join(result['completed_tasks'])}")
    print("  T4.3 executed: " + ("YES" if "T4.3" in result["execution_results"] else "NO"))
    return result


def run_t4_3_task_graph() -> dict[str, Any]:
    """Execute only T4.3 through the registered LangGraph task nodes."""
    plan = ProjectPlan(
        project_name="Unity Agent T4.3 multi-wave validation",
        requirements=[],
        phases=[Phase(id=4, name="T4.3 gameplay validation", tasks=[
            Task(id="T4.2", description="Previously verified game flow prerequisite", dependencies=[]),
            Task(id="T4.3.PREP", description="Previously completed playable-scene preparation",
                 dependencies=["T4.2"]),
            Task(id="T4.3", description="Play and validate multiple production gameplay waves",
                 dependencies=["T4.3.PREP"]),
        ])],
        dependencies=[Dependency(task="T4.3.PREP", depends_on=["T4.2"]),
                      Dependency(task="T4.3", depends_on=["T4.3.PREP"])],
        assumptions=[],
    )
    initial_state: AgentState = {
        "plan": plan,
        "completed_tasks": ["T4.2", "T4.3.PREP"],
        "available_tasks": [],
        "current_task": None,
        "execution_results": {"T4.3.PREP": "success"},
        "tool_results": [],
        "failed_tasks": [],
        "execution_error": None,
    }
    result = build_agent_graph().invoke(initial_state)
    print("LangGraph T4.3 result:")
    print(f"  T4.3: {result['execution_results'].get('T4.3', 'not run')}")
    print(f"  T4.3.PREP retained complete: {'T4.3.PREP' in result['completed_tasks']}")
    return result


def run_phase2_recovery_checkpoint() -> dict[str, Any]:
    """Exercise one real compile failure, diagnosis, bounded repair, and cleanup."""
    plan = ProjectPlan(
        project_name="Unity Agent controlled recovery checkpoint", requirements=[],
        phases=[Phase(id=2, name="Runtime recovery", tasks=[Task(
            id="PHASE2.RECOVERY", description="Recover an isolated compiler fixture",
            dependencies=[])])], dependencies=[], assumptions=[],
    )
    initial_state: AgentState = {
        "plan": plan, "completed_tasks": [], "available_tasks": [], "current_task": None,
        "execution_results": {}, "tool_results": [], "failed_tasks": [], "execution_error": None,
        "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
    }
    result = build_agent_graph().invoke(initial_state)
    print("Phase 2 controlled recovery result:")
    for item in result.get("validation_history", []):
        print(f"  Attempt {item['attempt_number']}: {item['failure_type']} — {item['summary']}")
    diagnosis = result.get("failure_diagnosis") or {}
    print(f"  Diagnosis source: {diagnosis.get('diagnosis_source', 'unknown')}")
    if diagnosis.get("llm_error"):
        print(f"  Gemini diagnosis error: {diagnosis['llm_error']}")
    print(f"  Diagnosis: {diagnosis.get('root_cause', 'none')}")
    if diagnosis.get("uncertainty"):
        print(f"  Diagnosis validation detail: {diagnosis['uncertainty']}")
    print(f"  Repair: {(result.get('repair_action') or {}).get('summary', 'none')}")
    print(f"  Repair attempts: {result.get('repair_attempts', 0)}")
    print(f"  Fixture removed: {(result.get('repair_action') or {}).get('fixture_removed', False)}")
    print(f"  Task result: {result.get('execution_results', {}).get('PHASE2.RECOVERY', 'not run')}")
    return result


T42_RUNTIME_TEST_ASMDEF = '''{
  "name": "T42RuntimeValidation",
  "references": [],
  "includePlatforms": [],
  "excludePlatforms": [],
  "allowUnsafeCode": false,
  "overrideReferences": false,
  "precompiledReferences": [],
  "autoReferenced": false,
  "defineConstraints": [],
  "versionDefines": [],
  "noEngineReferences": false,
  "optionalUnityReferences": ["TestAssemblies"]
}
'''


T42_RUNTIME_TEST_SOURCE = '''using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public class T42RuntimeValidationTests
{
    private sealed class TestWorld
    {
        public GameObject playerObject;
        public Component playerHealth;
        public Component playerWeapon;
        public GameObject enemyPrefab;
        public GameObject waveObject;
        public Component waveManager;
        public Component scoreManager;
    }

    [UnityTest]
    public IEnumerator RuntimeTransitionsAndRestartReset()
    {
        List<string> failures = new List<string>();
        if (SceneManager.GetActiveScene().name != "SampleScene")
            SceneManager.LoadScene("SampleScene");

        object manager = null;
        for (int frame = 0; frame < 300; frame++)
        {
            yield return null;
            manager = GetManager();
            if (manager != null && GetState(manager) == "Start" && Mathf.Approximately(Time.timeScale, 0f))
                break;
        }

        Record(failures, "Initial state -> Start",
            manager != null && GetState(manager) == "Start" && Mathf.Approximately(Time.timeScale, 0f));
        if (manager == null)
            Assert.Fail("GameStateManager did not bootstrap in Play mode.");

        TestWorld firstWorld = CreateWorld();
        Invoke(manager, "StartGame");
        Record(failures, "Start -> Playing",
            GetState(manager) == "Playing" && Mathf.Approximately(Time.timeScale, 1f));

        yield return null;
        int firstWave = GetInt(firstWorld.waveManager, "CurrentWave");
        int firstActiveCount = GetInt(firstWorld.waveManager, "ActiveEnemyCount");
        Invoke(firstWorld.waveManager, "BeginWaves");
        Invoke(firstWorld.waveManager, "BeginWaves");
        yield return new WaitForSecondsRealtime(0.1f);
        bool initialWaveStartedOnce = firstWave == 1
            && firstActiveCount == 1
            && GetInt(firstWorld.waveManager, "CurrentWave") == 1
            && GetInt(firstWorld.waveManager, "ActiveEnemyCount") == 1;

        Invoke(manager, "Pause");
        Record(failures, "Playing -> Paused",
            GetState(manager) == "Paused" && Mathf.Approximately(Time.timeScale, 0f));

        Invoke(manager, "Resume");
        Record(failures, "Paused -> Playing",
            GetState(manager) == "Playing" && Mathf.Approximately(Time.timeScale, 1f));

        int magazineSize = GetInt(firstWorld.playerWeapon, "MagazineSize");
        Invoke(firstWorld.playerWeapon, "Fire");
        float maxHealth = GetFloat(firstWorld.playerHealth, "MaxHealth");
        Invoke(firstWorld.playerHealth, "TakeDamage", 15f);
        Invoke(firstWorld.scoreManager, "AwardPoints", 75);
        bool changedBeforeRestart = GetInt(firstWorld.playerWeapon, "CurrentAmmo") < magazineSize
            && GetFloat(firstWorld.playerHealth, "CurrentHealth") < maxHealth
            && GetInt(firstWorld.scoreManager, "CurrentScore") == 75;

        float remainingHealth = GetFloat(firstWorld.playerHealth, "CurrentHealth");
        Invoke(firstWorld.playerHealth, "TakeDamage", remainingHealth + 1f);
        Record(failures, "Player Death -> GameOver",
            GetBool(firstWorld.playerHealth, "IsDead")
            && GetState(manager) == "GameOver"
            && Mathf.Approximately(Time.timeScale, 0f));

        int sceneLoads = 0;
        void OnSceneLoaded(Scene scene, LoadSceneMode mode) { sceneLoads++; }
        SceneManager.sceneLoaded += OnSceneLoaded;
        Invoke(manager, "Restart");
        for (int frame = 0; frame < 300 && sceneLoads == 0; frame++)
            yield return null;
        SceneManager.sceneLoaded -= OnSceneLoaded;

        bool restartedToStart = sceneLoads > 0
            && GetState(manager) == "Start"
            && Mathf.Approximately(Time.timeScale, 0f);
        Record(failures, "GameOver -> Restart", restartedToStart);
        Record(failures, "Post-restart state",
            restartedToStart && SceneManager.GetActiveScene().name == "SampleScene");
        Record(failures, "Time scale reset", restartedToStart && Mathf.Approximately(Time.timeScale, 0f));

        yield return new WaitForSecondsRealtime(0.1f);
        int managerCount = CountComponents("GameStateManager");
        int hudCount = CountComponents("GameHUD");
        Record(failures, "Duplicate manager/HUD check", managerCount == 1 && hudCount == 1,
            "GameStateManager=" + managerCount + ", GameHUD=" + hudCount);

        TestWorld restartedWorld = CreateWorld();
        bool statsReset = Mathf.Approximately(GetFloat(restartedWorld.playerHealth, "CurrentHealth"),
                GetFloat(restartedWorld.playerHealth, "MaxHealth"))
            && GetInt(restartedWorld.playerWeapon, "CurrentAmmo")
                == GetInt(restartedWorld.playerWeapon, "MagazineSize")
            && GetInt(restartedWorld.scoreManager, "CurrentScore") == 0
            && GetInt(restartedWorld.waveManager, "CurrentWave") == 0
            && changedBeforeRestart;
        Record(failures, "Score/health/ammo reset", statsReset);

        Invoke(manager, "StartGame");
        Invoke(restartedWorld.waveManager, "BeginWaves");
        Invoke(restartedWorld.waveManager, "BeginWaves");
        yield return new WaitForSecondsRealtime(0.1f);
        bool restartedWaveOnce = GetState(manager) == "Playing"
            && Mathf.Approximately(Time.timeScale, 1f)
            && GetInt(restartedWorld.waveManager, "CurrentWave") == 1
            && GetInt(restartedWorld.waveManager, "ActiveEnemyCount") == 1;
        Record(failures, "Wave restart check", initialWaveStartedOnce && restartedWaveOnce,
            "initial wave count=" + firstWave + ", restarted wave count="
            + GetInt(restartedWorld.waveManager, "CurrentWave") + ", active="
            + GetInt(restartedWorld.waveManager, "ActiveEnemyCount"));

        DestroyTestWorld(restartedWorld);
        Time.timeScale = 1f;
        Assert.That(failures, Is.Empty, string.Join("; ", failures));
    }

    private static TestWorld CreateWorld()
    {
        Type healthType = RuntimeType("PlayerHealth");
        Type weaponType = RuntimeType("PlayerWeapon");
        Type waveType = RuntimeType("WaveManager");
        Type scoreType = RuntimeType("ScoreManager");
        Type enemyHealthType = RuntimeType("EnemyHealth");

        TestWorld world = new TestWorld();
        world.playerObject = new GameObject("T4.2 Test Player");
        world.playerHealth = world.playerObject.AddComponent(healthType);
        world.playerWeapon = world.playerObject.AddComponent(weaponType);

        if (Camera.main == null)
        {
            GameObject cameraObject = new GameObject("T4.2 Test Camera");
            cameraObject.tag = "MainCamera";
            cameraObject.AddComponent<Camera>();
        }

        world.enemyPrefab = new GameObject("T4.2 Test Enemy Prefab");
        world.enemyPrefab.AddComponent(enemyHealthType);
        world.waveObject = new GameObject("T4.2 Test Wave Manager");
        world.waveObject.transform.position = new Vector3(100f, 0f, 0f);
        world.waveManager = world.waveObject.AddComponent(waveType);
        SetField(world.waveManager, "enemyPrefab", world.enemyPrefab);
        SetField(world.waveManager, "startingEnemies", 1);
        SetField(world.waveManager, "spawnInterval", 0f);
        SetField(world.waveManager, "timeBetweenWaves", 120f);
        world.scoreManager = world.waveObject.GetComponent(scoreType);
        return world;
    }

    private static void DestroyTestWorld(TestWorld world)
    {
        if (world.playerObject != null) UnityEngine.Object.Destroy(world.playerObject);
        if (world.enemyPrefab != null) UnityEngine.Object.Destroy(world.enemyPrefab);
        if (world.waveObject != null) UnityEngine.Object.Destroy(world.waveObject);
        foreach (MonoBehaviour behaviour in UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None))
        {
            if (behaviour != null && behaviour.gameObject.name.StartsWith("T4.2 Test Enemy Prefab(Clone)"))
                UnityEngine.Object.Destroy(behaviour.gameObject);
        }
    }

    private static object GetManager()
    {
        Type type = RuntimeType("GameStateManager");
        return type.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static).GetValue(null);
    }

    private static string GetState(object manager)
    {
        return manager.GetType().GetProperty("CurrentState").GetValue(manager).ToString();
    }

    private static int CountComponents(string typeName)
    {
        Type type = RuntimeType(typeName);
        return UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .Count(component => component != null && component.GetType() == type);
    }

    private static Type RuntimeType(string typeName)
    {
        foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type type = assembly.GetType(typeName);
            if (type != null) return type;
        }
        throw new InvalidOperationException("Could not find runtime type " + typeName);
    }

    private static object Invoke(object target, string methodName, params object[] arguments)
    {
        return target.GetType().GetMethod(methodName, BindingFlags.Public | BindingFlags.Instance)
            .Invoke(target, arguments);
    }

    private static object GetProperty(object target, string propertyName)
    {
        return target.GetType().GetProperty(propertyName, BindingFlags.Public | BindingFlags.Instance)
            .GetValue(target);
    }

    private static int GetInt(object target, string propertyName)
    {
        return Convert.ToInt32(GetProperty(target, propertyName));
    }

    private static float GetFloat(object target, string propertyName)
    {
        return Convert.ToSingle(GetProperty(target, propertyName));
    }

    private static bool GetBool(object target, string propertyName)
    {
        return Convert.ToBoolean(GetProperty(target, propertyName));
    }

    private static void SetField(object target, string fieldName, object value)
    {
        target.GetType().GetField(fieldName, BindingFlags.NonPublic | BindingFlags.Instance)
            .SetValue(target, value);
    }

    private static void Record(List<string> failures, string name, bool passed, string details = "")
    {
        string line = "[T4.2 RUNTIME] " + name + ": " + (passed ? "PASS" : "FAIL");
        if (!string.IsNullOrEmpty(details)) line += " - " + details;
        Debug.Log(line);
        if (!passed) failures.Add(name);
    }
}
'''


def run_t42_runtime_checkpoint() -> dict[str, Any]:
    """Install the isolated PlayMode test assembly and report its real runtime results."""
    report: dict[str, Any] = {
        "success": False,
        "files_created": [],
        "files_deployed": [],
        "files_verified": [],
    }
    validation = TOOLS["validate_unity_project"]()
    report["project_validation"] = validation
    if not validation.get("success"):
        report["error"] = validation.get("error")
        return report

    workspace_files = [
        ("UnityProject/Assets/Tests/PlayMode/T42RuntimeValidation.asmdef", T42_RUNTIME_TEST_ASMDEF),
        ("UnityProject/Assets/Tests/PlayMode/T42RuntimeValidationTests.cs", T42_RUNTIME_TEST_SOURCE),
    ]
    for path, source in workspace_files:
        created = _run_tool("create_file", path, source)
        if not created.get("success"):
            report["error"] = f"Could not create test file {path}: {created.get('error')}"
            return report
        readback = _run_tool("read_file", path)
        if not readback.get("success") or readback.get("content") != source:
            report["error"] = f"Workspace test file verification failed for {path}"
            return report
        report["files_created"].append(path)

    for path, source in workspace_files:
        relative_path = "Assets/Tests/PlayMode/" + Path(path).name
        deployed = unity_create_file(relative_path, source)
        if not deployed.get("success"):
            report["error"] = f"Test bridge deployment failed for {relative_path}: {deployed.get('error')}"
            return report
        exists = TOOLS["unity_file_exists"](relative_path)
        readback = TOOLS["unity_read_file"](relative_path) if exists.get("exists") else None
        if (not exists.get("success") or not exists.get("exists") or readback is None
                or not readback.get("success") or readback.get("content") != source):
            report["error"] = f"Deployed test bridge verification failed for {relative_path}"
            return report
        report["files_deployed"].append(relative_path)
        report["files_verified"].append(relative_path)

    runtime_result = run_unity_t42_playmode_validation()
    report["runtime_validation"] = runtime_result
    report["unity_exit_code"] = runtime_result.get("exit_code")
    report["runtime_checks"] = runtime_result.get("runtime_checks", [])
    report["compiler_errors"] = runtime_result.get("compiler_errors", [])
    report["success"] = runtime_result.get("success", False)

    print("T4.2 PlayMode runtime validation:")
    for check in report["runtime_checks"]:
        detail = f" - {check['details']}" if check.get("details") else ""
        print(f"  {check['check']}: {check['result']}{detail}")
    for missing in runtime_result.get("missing_runtime_checks", []):
        print(f"  {missing}: NOT REPORTED")
    print(f"  Unity exit code: {report['unity_exit_code']}")
    print(f"  C# compiler errors: {len(report['compiler_errors'])}")
    print(f"  PlayMode test cases: {runtime_result.get('test_total', 0)}")
    print(f"  Result: {'PASS' if report['success'] else 'FAIL'}")
    for failure in runtime_result.get("test_failures", []):
        print(f"  Test failure: {failure}")
    for error in report["compiler_errors"]:
        print(f"  Compiler error: {error}")
    for message in runtime_result.get("unity_messages", []):
        print(f"  Unity message: {message}")
    if not report["success"]:
        for label in ("stderr", "stdout", "unity_log"):
            detail = runtime_result.get(label, "")
            if detail:
                print(f"  Unity {label} tail:")
                print(detail[-6000:])
    return report


def main():
    parser = argparse.ArgumentParser(description="Unity AI development planner and executor")
    parser.add_argument(
        "--unity-integration-test",
        action="store_true",
        help="Test safe deployment and optional batch compilation using configured Unity paths",
    )
    parser.add_argument(
        "--unity-gameplay-checkpoint",
        action="store_true",
        help="Deploy and compile the seven currently implemented gameplay scripts",
    )
    parser.add_argument(
        "--t4-2-runtime-validation",
        action="store_true",
        help="Run the isolated T4.2 PlayMode transition and restart validation",
    )
    parser.add_argument(
        "--prepare-playable-scene",
        action="store_true",
        help="Assemble SampleScene from existing gameplay scripts and run its smoke test",
    )
    parser.add_argument(
        "--t4-3-runtime-validation",
        action="store_true",
        help="Run the T4.3 inspect, deploy, compile, and multi-wave PlayMode executor",
    )
    parser.add_argument(
        "--phase2-recovery-checkpoint",
        action="store_true",
        help="Run the controlled diagnosis/recovery fixture, then T4.3 regression",
    )
    parser.add_argument(
        "--plan-game",
        action="store_true",
        help="Generate and display a capability-driven plan for a natural-language game idea",
    )
    parser.add_argument(
        "--build-game",
        action="store_true",
        help="Generate a capability plan and execute it through LangGraph",
    )
    parser.add_argument(
        "--game-idea",
        help="Natural-language game idea used with --plan-game or --build-game",
    )
    args = parser.parse_args()
    if args.phase2_recovery_checkpoint:
        recovery = run_phase2_recovery_checkpoint()
        if recovery.get("execution_results", {}).get("PHASE2.RECOVERY") != "success":
            raise SystemExit(recovery.get("execution_error") or "Controlled recovery checkpoint failed.")
        regression = run_t4_3_task_graph()
        if regression.get("execution_results", {}).get("T4.3") != "success":
            raise SystemExit(regression.get("execution_error") or "T4.3 regression failed.")
        return
    if args.prepare_playable_scene:
        result = run_t4_3_preparation_task_graph()
        if result.get("execution_results", {}).get("T4.3.PREP") != "success":
            raise SystemExit(result.get("execution_error") or "SampleScene preparation failed.")
        return
    if args.t4_3_runtime_validation:
        result = run_t4_3_task_graph()
        if result.get("execution_results", {}).get("T4.3") != "success":
            raise SystemExit(result.get("execution_error") or "T4.3 validation failed.")
        return
    if args.t4_2_runtime_validation:
        run_t42_runtime_checkpoint()
        return
    if args.unity_gameplay_checkpoint:
        run_gameplay_checkpoint(read_file)
        return
    if args.unity_integration_test:
        run_player_health_integration_test(PLAYER_HEALTH_SOURCE)
        return

    if args.build_game:
        game_idea = args.game_idea or input("Describe your game idea: ").strip()
        if not game_idea:
            raise SystemExit("Please provide a non-empty --game-idea.")
        result = run_capability_game_build(game_idea)
        if result.get("failed_tasks"):
            raise SystemExit(result.get("execution_error") or "Capability-driven Unity build failed.")
        return

    if args.plan_game:
        game_idea = args.game_idea or input("Describe your game idea: ").strip()
        if not game_idea:
            raise SystemExit("Please enter a game idea before submitting.")
        plan = ProjectPlan.model_validate(build_capability_plan(game_idea))
    elif OFFLINE_MODE:
        print("========== PLAN SOURCE ==========")
        print("Using local sample_plan.json")
        try:
            plan = load_plan_from_json(SAMPLE_PLAN_PATH)
        except (OSError, ValidationError, ValueError) as exc:
            raise SystemExit(f"Could not load local plan from {SAMPLE_PLAN_PATH}: {exc}")
    else:
        # Read the API key only for online mode; offline mode never initializes Gemini.
        if not os.getenv("GEMINI_API_KEY"):
            raise SystemExit(
                "GEMINI_API_KEY is not set. In PowerShell, run: "
                '$env:GEMINI_API_KEY="your_api_key_here"'
            )

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        # Ask for the game idea and stop early if the user enters a blank line.
        prompt = input("Describe your game idea: ")
        if not prompt.strip():
            raise SystemExit("Please enter a game idea before submitting.")

        # The response schema controls structure; Gemini supplies the plan content.
        contents = f"""
You are a Unity game development planning agent. Turn the user's game idea into
a clear development plan for a beginner. The idea may be vague, so infer likely
requirements and state any assumptions you make.

Do not generate implementation code, scripts, or code snippets yet.
Create a concise project name, list likely gameplay requirements, arrange tasks
into logical phases in dependency order, record task dependencies, and state
any assumptions. Include Unity project and scene setup where appropriate.
Give each task a stable ID in the format T<phase number>.<task number>, such as
T1.1 and T1.2. Use those same IDs in each task's dependencies.

User's game idea:

{prompt}
"""

        # Retry temporary service and rate-limit errors, then show a short message.
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = client.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ProjectPlan,
                    ),
                )
                break
            except errors.APIError as exc:
                if exc.code in TEMPORARY_ERROR_CODES:
                    if attempt < MAX_ATTEMPTS:
                        print("Gemini is temporarily busy. Retrying...")
                        time.sleep(RETRY_DELAY_SECONDS)
                        continue

                    print(
                        f"Gemini is still temporarily unavailable after {MAX_ATTEMPTS} "
                        "attempts. Please try again later."
                    )
                else:
                    print(f"Gemini API error ({exc.code}): {exc.message}")
                    if exc.code == 404:
                        print("Check that the model ID is available to your Gemini API account.")
                    elif exc.code in (401, 403):
                        print("Check that GEMINI_API_KEY is valid and has Gemini API access.")
                return

        if not response.text:
            print("Gemini returned no text. Try a different prompt.")
            return

        # Validate the online response using the existing ProjectPlan schema.
        try:
            plan = ProjectPlan.model_validate_json(response.text)
        except ValidationError as exc:
            print("Gemini returned structured JSON that failed ProjectPlan validation:")
            print(exc)
            return

    print("\n========== DEVELOPMENT PLAN ==========")
    print(f"Project name: {plan.project_name}")
    if plan.selected_capabilities:
        print(f"Genre: {plan.genre}")
        print(f"Camera perspective: {plan.camera_perspective}")
        print(f"Core loop: {plan.core_loop}")
        print("Selected capabilities: " + ", ".join(plan.selected_capabilities))
        print("Controls: " + ", ".join(plan.controls))
        print("Game objects: " + ", ".join(plan.game_objects))
        print("Scene requirements: " + ", ".join(plan.scene_requirements))
        print("Acceptance criteria:")
        for criterion in plan.acceptance_criteria:
            print(f"  - {criterion}")
        if plan.missing_information:
            print("Missing information (assumptions applied):")
            for item in plan.missing_information:
                print(f"  - {item}")

    print("\nPhases and tasks:")
    for phase in plan.phases:
        print(f"\nPhase {phase.id}: {phase.name}")
        for task in phase.tasks:
            task_dependencies = ", ".join(task.dependencies) or "none"
            print(f"  - {task.id}: {task.description} (depends on: {task_dependencies})")

    print("\nDependencies:")
    if plan.dependencies:
        for dependency in plan.dependencies:
            depends_on = ", ".join(dependency.depends_on) or "none"
            print(f"  - {dependency.task} depends on: {depends_on}")
    else:
        print("  - none")

    print("\nAssumptions:")
    if plan.assumptions:
        for assumption in plan.assumptions:
            print(f"  - {assumption}")
    else:
        print("  - none")

    print("\n===== RAW STRUCTURED JSON =====")
    print(plan.model_dump_json(indent=2))

    if args.plan_game:
        print("\nCapability plan generated. Execution is not enabled for capability tasks yet.")
        return

    # Information flows through the graph in this state, updated by each node.
    initial_state: AgentState = {
        "plan": plan,
        "completed_tasks": [],
        "available_tasks": [],
        "current_task": None,
        "execution_results": {},
        "tool_results": [],
        "failed_tasks": [],
    }
    result = build_agent_graph().invoke(initial_state)

    print("\n========== AGENT FINISHED ==========")
    print("\nCompleted tasks:")
    for task_id in result["completed_tasks"]:
        print(task_id)
    failed_tasks = [task_id for task_id, status in result["execution_results"].items() if status == "failed"]
    print("\nFailed tasks:")
    for task_id in failed_tasks:
        print(task_id)
        print(result.get("execution_error", "One or more workspace tools failed."))
    print("\nExecution results:")
    for task_id, status in result["execution_results"].items():
        print(f"{task_id}: {status}")
    print("\nTool results:")
    for tool_result in result["tool_results"]:
        print(tool_result)
    print("\nWorkspace:")
    print("agent_workspace/")


if __name__ == "__main__":
    main()
