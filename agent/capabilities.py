"""Capability catalog and deterministic game-idea-to-plan compiler."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GameplayCapability:
    id: str
    name: str
    category: str
    task_description: str
    prerequisites: tuple[str, ...]
    triggers: tuple[str, ...]
    interfaces: tuple[str, ...]
    configuration_options: tuple[str, ...]
    scene_requirements: tuple[str, ...]
    validation_requirements: tuple[str, ...]


CAPABILITY_REGISTRY: dict[str, GameplayCapability] = {
    capability.id: capability
    for capability in (
        GameplayCapability(
            id="scene.primitives",
            name="Primitive scene foundation",
            category="Foundation",
            task_description="Create a dedicated scene foundation with placeholder geometry, lighting, and a player spawn.",
            prerequisites=(),
            triggers=(),
            interfaces=("Unity Scene", "GameObject", "Transform"),
            configuration_options=("scene name", "ground dimensions", "lighting intensity"),
            scene_requirements=("Dedicated scene", "Ground surface", "Directional light", "Player spawn point"),
            validation_requirements=("Scene asset exists", "Ground and player spawn are present"),
        ),
        GameplayCapability(
            id="movement.first_person",
            name="First-person movement",
            category="Player",
            task_description="Implement first-person player movement and look controls using Unity CharacterController.",
            prerequisites=("scene.primitives",),
            triggers=("first-person", "first person", "fps"),
            interfaces=("CharacterController", "PlayerInput"),
            configuration_options=("walk speed", "look sensitivity", "gravity"),
            scene_requirements=("Player GameObject", "CharacterController", "First-person camera"),
            validation_requirements=("Movement input changes player position", "Look input updates camera orientation"),
        ),
        GameplayCapability(
            id="movement.third_person",
            name="Third-person movement",
            category="Player",
            task_description="Implement third-person player movement and camera-relative controls.",
            prerequisites=("scene.primitives",),
            triggers=("third-person", "third person", "over-the-shoulder", "over the shoulder"),
            interfaces=("CharacterController", "PlayerInput", "CameraFollow"),
            configuration_options=("walk speed", "rotation speed", "camera distance", "gravity"),
            scene_requirements=("Player GameObject", "CharacterController", "Follow camera"),
            validation_requirements=("Movement input changes player position", "Camera follows the player"),
        ),
        GameplayCapability(
            id="movement.simple_2d",
            name="Simple 2D movement",
            category="Player",
            task_description="Implement simple 2D player movement for a side-on or top-down prototype.",
            prerequisites=("scene.primitives",),
            triggers=("2d", "side-scrolling", "side scrolling", "side-scroller", "side scroller"),
            interfaces=("Rigidbody2D", "Collider2D", "PlayerInput"),
            configuration_options=("movement speed", "jump force", "gravity scale"),
            scene_requirements=("2D player GameObject", "Rigidbody2D", "Collider2D", "2D camera"),
            validation_requirements=("Movement input changes player position", "Player remains in the 2D play plane"),
        ),
        GameplayCapability(
            id="camera.control",
            name="Camera control",
            category="Player",
            task_description="Configure a camera that follows and frames the selected player perspective.",
            prerequisites=(),
            triggers=(),
            interfaces=("Camera", "Transform"),
            configuration_options=("follow offset", "look sensitivity", "camera bounds"),
            scene_requirements=("Main Camera", "Player camera target"),
            validation_requirements=("Camera reference resolves", "Camera follows or frames the player"),
        ),
        GameplayCapability(
            id="player.health",
            name="Player health and damage",
            category="Gameplay",
            task_description="Add player health, damage handling, and a one-time defeat state.",
            prerequisites=("scene.primitives",),
            triggers=("health", "damage", "combat", "death", "survive"),
            interfaces=("CurrentHealth", "TakeDamage", "Died"),
            configuration_options=("maximum health", "damage response"),
            scene_requirements=("Player health component",),
            validation_requirements=("Damage lowers health", "Defeat is raised once at zero health"),
        ),
        GameplayCapability(
            id="enemy.basic",
            name="Enemy health and basic AI",
            category="Gameplay",
            task_description="Add a simple enemy with health and a bounded pursuit or attack behavior.",
            prerequisites=("scene.primitives", "player.health"),
            triggers=("enemy", "enemies", "monster", "monsters", "hostile", "zombie", "zombies"),
            interfaces=("EnemyHealth", "TakeDamage", "EnemyController"),
            configuration_options=("enemy health", "movement speed", "attack range", "attack damage"),
            scene_requirements=("Enemy prefab", "Enemy health component", "Enemy controller"),
            validation_requirements=("Enemy can be instantiated", "Enemy behavior responds to the player"),
        ),
        GameplayCapability(
            id="weapon.projectile",
            name="Weapon and projectiles",
            category="Gameplay",
            task_description="Add a configurable player weapon that creates a projectile and applies damage.",
            prerequisites=("scene.primitives",),
            triggers=("weapon", "weapons", "shoot", "shooting", "fire", "projectile", "projectiles"),
            interfaces=("Fire", "Projectile", "IDamageable"),
            configuration_options=("projectile speed", "damage", "fire interval"),
            scene_requirements=("Weapon input", "Projectile prefab"),
            validation_requirements=("Firing creates a projectile", "Projectile collision applies damage"),
        ),
        GameplayCapability(
            id="collectibles.counter",
            name="Collectibles and counter",
            category="Gameplay",
            task_description="Add collectible objects and a run-scoped counter that updates exactly once per pickup.",
            prerequisites=("scene.primitives",),
            triggers=("collect", "collectible", "collectibles", "coin", "coins", "gem", "gems", "pickup", "pick up", "treasure"),
            interfaces=("Collectible", "Collect", "CurrentCount", "CountChanged"),
            configuration_options=("required count", "pickup radius", "score per item"),
            scene_requirements=("Collectible prefab", "Configured collectible instances"),
            validation_requirements=("Player can collect an item", "Counter increments once per item"),
        ),
        GameplayCapability(
            id="objectives.collect_all",
            name="Collect-all objective",
            category="Objectives",
            task_description="Complete the objective when the required collectibles are gathered and the finish is reached.",
            prerequisites=("collectibles.counter",),
            triggers=("finish", "finish line", "exit", "goal", "all items", "all collectibles", "collect everything"),
            interfaces=("ObjectiveComplete", "TryFinish"),
            configuration_options=("finish trigger size", "required collectible count"),
            scene_requirements=("Finish trigger", "Objective reference to the collectible counter"),
            validation_requirements=("Finish remains locked before the required count", "Finish completes after all required items"),
        ),
        GameplayCapability(
            id="objectives.timer",
            name="Timer and timed objective",
            category="Objectives",
            task_description="Add a bounded game timer and evaluate the configured time objective.",
            prerequisites=("scene.primitives",),
            triggers=("timer", "timed", "time limit", "countdown", "race against time"),
            interfaces=("RemainingTime", "TimeExpired"),
            configuration_options=("duration", "count direction", "expiry behavior"),
            scene_requirements=("Timer owner",),
            validation_requirements=("Timer changes with elapsed gameplay time", "Expiry is raised once"),
        ),
        GameplayCapability(
            id="spawning.waves",
            name="Wave and spawn management",
            category="Gameplay",
            task_description="Spawn configured enemy waves and advance only after each wave is cleared.",
            prerequisites=("enemy.basic",),
            triggers=("wave", "waves", "spawn", "spawning", "horde"),
            interfaces=("BeginWaves", "CurrentWave", "WaveCleared"),
            configuration_options=("starting enemies", "additional enemies", "spawn interval"),
            scene_requirements=("Wave manager", "Enemy prefab", "Spawn points"),
            validation_requirements=("Waves start once", "Enemy counts follow configuration"),
        ),
        GameplayCapability(
            id="game_state.core",
            name="Game state and restart",
            category="Interface",
            task_description="Manage start, playing, pause, completion, and scene restart transitions.",
            prerequisites=("scene.primitives",),
            triggers=(),
            interfaces=("GameState", "StartGame", "Pause", "Resume", "Restart"),
            configuration_options=("initial state", "restart scene"),
            scene_requirements=("Game state manager", "Valid restart scene"),
            validation_requirements=("Start and pause transitions are valid", "Restart restores initial state"),
        ),
        GameplayCapability(
            id="ui.hud",
            name="Basic HUD",
            category="Interface",
            task_description="Show the selected gameplay status and provide basic start, restart, or objective feedback.",
            prerequisites=("game_state.core",),
            triggers=(),
            interfaces=("HUD", "StatusLabel"),
            configuration_options=("displayed counters", "HUD layout"),
            scene_requirements=("Runtime or scene-authored HUD",),
            validation_requirements=("HUD resolves selected gameplay data", "Displayed status changes with gameplay"),
        ),
        GameplayCapability(
            id="checkpoints.transitions",
            name="Checkpoints and scene transitions",
            category="Interface",
            task_description="Save a checkpoint and transition between explicitly configured scenes.",
            prerequisites=("scene.primitives", "game_state.core"),
            triggers=("checkpoint", "checkpoints", "level transition", "next level", "scene transition"),
            interfaces=("Checkpoint", "LoadScene"),
            configuration_options=("checkpoint spawn", "destination scene"),
            scene_requirements=("Checkpoint trigger", "Configured destination scene"),
            validation_requirements=("Checkpoint activation is observed", "Transition target is valid"),
        ),
        GameplayCapability(
            id="construction.assemble",
            name="Assemble and verify playable game",
            category="Validation",
            task_description="Assemble the selected gameplay systems into a dedicated Unity scene and verify the playable result.",
            prerequisites=(),
            triggers=(),
            interfaces=("Unity Editor construction API", "PlayMode test assembly"),
            configuration_options=("scene path", "generated asset directory"),
            scene_requirements=("Dedicated scene", "Verified gameplay references"),
            validation_requirements=("Unity compilation passes", "Gameplay PlayMode acceptance tests pass"),
        ),
    )
}


_CAPABILITY_ORDER = tuple(CAPABILITY_REGISTRY)
_PHASE_ORDER = {
    "Foundation": 1,
    "Player": 2,
    "Gameplay": 3,
    "Objectives": 4,
    "Interface": 5,
    "Validation": 6,
}
_OBJECT_TERMS = (
    ("player", ("player", "character", "avatar")),
    ("obstacles", ("obstacle", "barrier", "platform")),
    ("collectibles", ("collectible", "coin", "gem", "treasure", "pickup", "pick up")),
    ("enemies", ("enemy", "enemies", "monster", "zombie", "hostile")),
    ("projectiles", ("projectile", "weapon", "shoot", "fire")),
    ("finish", ("finish", "finish line", "exit", "goal")),
    ("checkpoints", ("checkpoint",)),
)


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _select_capabilities(text: str) -> set[str]:
    selected = {
        "scene.primitives",
        "game_state.core",
        "ui.hud",
        "construction.assemble",
    }
    is_2d = _contains_any(text, ("2d", "side-scrolling", "side scrolling", "side-scroller", "side scroller"))
    if is_2d:
        selected.add("movement.simple_2d")
    elif _contains_any(text, ("first-person", "first person", "fps")):
        selected.add("movement.first_person")
    else:
        selected.add("movement.third_person")

    selected.add("camera.control")
    if _contains_any(text, CAPABILITY_REGISTRY["player.health"].triggers):
        selected.add("player.health")
    if _contains_any(text, CAPABILITY_REGISTRY["enemy.basic"].triggers):
        selected.update(("enemy.basic", "player.health"))
    if _contains_any(text, CAPABILITY_REGISTRY["weapon.projectile"].triggers):
        selected.add("weapon.projectile")
    if _contains_any(text, CAPABILITY_REGISTRY["collectibles.counter"].triggers):
        selected.add("collectibles.counter")
    if ("collectibles.counter" in selected
            and _contains_any(text, CAPABILITY_REGISTRY["objectives.collect_all"].triggers)):
        selected.add("objectives.collect_all")
    if _contains_any(text, CAPABILITY_REGISTRY["objectives.timer"].triggers):
        selected.add("objectives.timer")
    if _contains_any(text, CAPABILITY_REGISTRY["spawning.waves"].triggers):
        selected.update(("spawning.waves", "enemy.basic", "player.health"))
    if _contains_any(text, CAPABILITY_REGISTRY["checkpoints.transitions"].triggers):
        selected.add("checkpoints.transitions")

    pending = list(selected)
    while pending:
        capability_id = pending.pop()
        for prerequisite in CAPABILITY_REGISTRY[capability_id].prerequisites:
            if prerequisite not in selected:
                selected.add(prerequisite)
                pending.append(prerequisite)
    return selected


def _topological_capabilities(selected: set[str]) -> list[GameplayCapability]:
    ordered: list[GameplayCapability] = []
    remaining = set(selected)
    while remaining:
        ready = [
            capability_id
            for capability_id in _CAPABILITY_ORDER
            if capability_id in remaining
            and set(
                _selected_prerequisites(CAPABILITY_REGISTRY[capability_id], selected)
            ).issubset(selected - remaining)
        ]
        if not ready:
            raise ValueError(f"Capability prerequisites contain a cycle: {sorted(remaining)}")
        for capability_id in ready:
            ordered.append(CAPABILITY_REGISTRY[capability_id])
            remaining.remove(capability_id)
    return ordered


def _selected_prerequisites(capability: GameplayCapability, selected: set[str]) -> tuple[str, ...]:
    prerequisites = list(capability.prerequisites)
    if capability.id == "camera.control":
        player_movement = next(
            (
                capability_id
                for capability_id in (
                    "movement.first_person",
                    "movement.third_person",
                    "movement.simple_2d",
                )
                if capability_id in selected
            ),
            None,
        )
        if player_movement:
            prerequisites.append(player_movement)
    return tuple(dict.fromkeys(prerequisites))


def _infer_genre(text: str, selected: set[str]) -> str:
    if "spawning.waves" in selected:
        return "wave-survival action"
    if "objectives.collect_all" in selected and "movement.simple_2d" not in selected:
        return "3D collectible obstacle course"
    if "objectives.collect_all" in selected:
        return "2D collectible platformer"
    if "collectibles.counter" in selected:
        return "collectible exploration"
    if "weapon.projectile" in selected or "enemy.basic" in selected:
        return "combat action"
    if "objectives.timer" in selected:
        return "timed challenge"
    if _contains_any(text, ("puzzle", "riddle", "logic game")):
        return "puzzle"
    return "exploration prototype"


def _infer_game_objects(text: str, selected: set[str]) -> list[str]:
    objects: list[str] = []
    for object_name, phrases in _OBJECT_TERMS:
        if _contains_any(text, phrases):
            objects.append(object_name)
    if "player" not in objects:
        objects.insert(0, "player")
    if "obstacles" not in objects and "scene.primitives" in selected:
        objects.append("obstacles and ground")
    if "collectibles.counter" in selected and "collectibles" not in objects:
        objects.append("collectibles")
    return objects


def _infer_controls(text: str, selected: set[str]) -> list[str]:
    controls = []
    if "movement.simple_2d" in selected:
        controls.append("directional movement and jump")
    else:
        controls.append("WASD movement")
        controls.append("mouse camera control")
        if "movement.third_person" in selected:
            controls.append("jump")
    if "weapon.projectile" in selected:
        controls.append("fire")
    if _contains_any(text, ("restart", "restartable")):
        controls.append("restart")
    return controls


def build_capability_plan(game_idea: str) -> dict:
    """Turn a natural-language prototype idea into a dependency-ordered plan."""
    original = game_idea.strip()
    if not original:
        raise ValueError("A game idea is required to build a capability plan.")

    normalized = re.sub(r"\s+", " ", original.casefold())
    selected = _select_capabilities(normalized)
    ordered = _topological_capabilities(selected)
    idea_title = re.split(r"\b(where|with|that|which)\b|[.!?]", original, maxsplit=1, flags=re.IGNORECASE)[0]
    idea_title = re.sub(r"^(build|create|make|design)\s+", "", idea_title, flags=re.IGNORECASE).strip()
    title_words = re.findall(r"[A-Za-z0-9]+", idea_title)[:8]
    project_name = " ".join(title_words).title() or "Unity Game Prototype"
    game_slug = re.sub(r"[^A-Za-z0-9]+", "", project_name) or "UnityGamePrototype"
    task_id_by_capability = {
        capability.id: f"CAP.{capability.id.replace('.', '_')}"
        for capability in ordered
    }
    tasks = []
    dependency_rows = []
    for capability in ordered:
        task_dependencies = [
            task_id_by_capability[prerequisite]
            for prerequisite in _selected_prerequisites(capability, selected)
            if prerequisite in selected
        ]
        if capability.id == "construction.assemble":
            task_dependencies = [
                task_id_by_capability[dependency.id]
                for dependency in ordered
                if dependency.id != capability.id
            ]
        task_id = task_id_by_capability[capability.id]
        tasks.append({
            "id": task_id,
            "description": capability.task_description,
            "dependencies": task_dependencies,
            "capability_id": capability.id,
            "inputs": {
                "capability_id": capability.id,
                "game_idea": original,
                "scene_path": f"Assets/UnityAgent/Generated/{game_slug}/Scenes/Main.unity",
            },
            "acceptance_criteria": list(capability.validation_requirements),
        })
        if task_dependencies:
            dependency_rows.append({"task": task_id, "depends_on": task_dependencies})

    phases_by_id: dict[int, list[dict]] = {}
    for task, capability in zip(tasks, ordered):
        phase_id = _PHASE_ORDER[capability.category]
        phases_by_id.setdefault(phase_id, []).append(task)
    phases = [
        {
            "id": phase_id,
            "name": next(
                capability.category
                for capability in ordered
                if _PHASE_ORDER[capability.category] == phase_id
            ),
            "tasks": phase_tasks,
        }
        for phase_id, phase_tasks in sorted(phases_by_id.items())
    ]

    perspective = (
        "2D"
        if "movement.simple_2d" in selected
        else "first-person"
        if "movement.first_person" in selected
        else "third-person"
    )
    genre = _infer_genre(normalized, selected)
    core_loop_parts = []
    if "movement.simple_2d" in selected or "movement.third_person" in selected or "movement.first_person" in selected:
        core_loop_parts.append("move through the environment")
    if "collectibles.counter" in selected:
        core_loop_parts.append("collect items")
    if "enemy.basic" in selected:
        core_loop_parts.append("engage enemies")
    if "spawning.waves" in selected:
        core_loop_parts.append("survive increasingly difficult waves")
    if "objectives.collect_all" in selected:
        core_loop_parts.append("reach the finish after collecting all required items")
    elif "objectives.timer" in selected:
        core_loop_parts.append("complete the objective before time expires")
    elif not any(part in core_loop_parts for part in ("engage enemies", "collect items")):
        core_loop_parts.append("explore the prototype scene")

    assumptions = [
        "Use primitive geometry and placeholder materials unless the idea specifies otherwise.",
        "Treat unspecified controls as keyboard and mouse for a desktop prototype.",
    ]
    if perspective == "third-person" and not _contains_any(
        normalized, ("third-person", "third person", "over-the-shoulder", "over the shoulder")
    ):
        assumptions.append("Use a third-person camera because no camera perspective was specified.")
    if "objectives.collect_all" in selected and not _contains_any(
        normalized, ("all", "every", "finish", "finish line", "exit", "goal")
    ):
        assumptions.append("Require all placed collectibles before allowing the finish trigger.")

    acceptance = list(dict.fromkeys(
        requirement
        for capability in ordered
        for requirement in capability.validation_requirements
    ))
    scene_requirements = list(dict.fromkeys(
        requirement
        for capability in ordered
        for requirement in capability.scene_requirements
    ))
    missing_information = []
    if not _contains_any(normalized, ("desktop", "pc", "mobile", "console", "web")):
        missing_information.append("Target platform is unspecified; assume desktop.")
    if "movement.simple_2d" not in selected and not _contains_any(
        normalized, ("first-person", "first person", "third-person", "third person", "over-the-shoulder")
    ):
        missing_information.append("Camera perspective is unspecified; assume third-person.")

    return {
        "project_name": project_name,
        "requirements": [original, *acceptance],
        "phases": phases,
        "dependencies": dependency_rows,
        "assumptions": assumptions,
        "genre": genre,
        "camera_perspective": perspective,
        "core_loop": " -> ".join(core_loop_parts),
        "controls": _infer_controls(normalized, selected),
        "game_objects": _infer_game_objects(normalized, selected),
        "selected_capabilities": [capability.id for capability in ordered],
        "scene_requirements": scene_requirements,
        "acceptance_criteria": acceptance,
        "missing_information": missing_information,
    }
