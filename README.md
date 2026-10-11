# Unity Agentic AI

A Python game-development agent that plans and executes bounded Unity tasks.
It combines a deterministic gameplay-capability planner, LangGraph task
execution and dependency resolution, an allowlisted Unity Editor construction
bridge, compilation and PlayMode validation, and evidence-based bounded
diagnosis/repair.

Phase 3 is complete. The primary generated sample is a 3D third-person
collectible obstacle course. The repository also includes the Urban Survival
production scene and its regression tests.

## Repository layout

- `agent/` — planning, LangGraph workflow, capability registry, Unity
  integration, and generated gameplay/test source templates.
- `agent_workspace/UnityProject/` — workspace-side source fixtures consumed by
  the agent's existing file tools. This is not the configured Unity project.
- `UnityProject/` — reproducible Unity project copy with production assets,
  required `.meta` files, project settings and package manifests, the tested
  Summit Crystal Trial scene, and PlayMode tests.
- `tests/` — Python unit tests for planning, construction, and path/component
  safety.
- `PHASE3_PROGRESS.md` — validation summary, manual confirmation, limitations,
  and retained report locations.

Unity caches, test-created temporary scenes, duplicate generated experiments,
construction probes, and local construction-bridge backups are intentionally
not part of the Unity project copy.

## Prerequisites

- Windows PowerShell.
- Python 3.14.x (the development environment used for the recorded validation
  was Python 3.14.3).
- Unity Editor **6000.3.25f1**. Required Unity packages are pinned by
  `UnityProject/Packages/manifest.json` and `packages-lock.json`.
- A Gemini API key only for online model-backed paths. The deterministic
  capability planner and offline workflows do not require a key.

## Python setup

From the repository root:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The dependencies are pinned to the versions used during Phase 3 validation.
The `.env.example` file documents configuration names and placeholder values;
the program does not automatically load `.env` files. Set values in the
PowerShell process environment as described below.

## Configure Unity and optional Gemini access

Set paths for the current PowerShell session. Update the Editor path if Unity
Hub installed the requested version elsewhere:

```powershell
$env:UNITY_PROJECT_PATH = (Resolve-Path .\UnityProject).Path
$env:UNITY_EDITOR_PATH = 'C:\Program Files\Unity\Hub\Editor\6000.3.25f1\Editor\Unity.exe'
$env:MAX_REPAIR_ATTEMPTS = '2'
```

For online Gemini features only, set the key securely in your shell or secret
manager. Do not put a real key in `.env.example`, source code, or Git:

```powershell
$env:GEMINI_API_KEY = 'your-own-key-from-a-secure-source'
```

No real credentials are included in this repository.

## Plan and build a game

Generate a capability-derived plan without changing Unity assets:

```powershell
python agent\main.py --plan-game --game-idea "A small 3D third-person course where the player collects gems and reaches the finish"
```

Execute a new idea through the normal LangGraph workflow:

```powershell
python agent\main.py --build-game --game-idea "A uniquely named 3D third-person course where the player collects gems and reaches the finish"
```

The planner derives selected capabilities and dependencies from the idea. The
workflow executes only ready tasks and validates generated files and gameplay
before marking tasks complete. Before building, check the plan's generated
`Assets/UnityAgent/Generated/<name>` destination. Construction refuses to
overwrite existing scene assets by default; use a new, unique idea/name rather
than deleting or replacing an earlier output.

The currently bounded construction executors cover primitive scene setup,
third-person movement, camera follow, collectibles, collect-all objectives,
game state, HUD, and scene assembly. Other registry entries are not necessarily
constructible yet.

## Open the sample in Unity

Open `UnityProject/` with Unity **6000.3.25f1**. To inspect the generated game,
open:

```text
Assets/UnityAgent/Generated/ASummitCrystalTrialIn3DThirdPerson/Scenes/Main.unity
```

The Urban Survival production scene remains at
`Assets/Scenes/SampleScene.unity`. Generated games are kept in a separate asset
directory.

`agent_workspace/UnityProject/` is an input/source fixture used by legacy agent
tasks; it is deliberately separate from the configured Unity project. Its
production gameplay files may differ from the actual project. Review any
proposed deployment diff before running legacy tasks that copy workspace files
into Unity. Never bulk-copy staging assets into production folders.

## Run Unity PlayMode tests

In PowerShell, after setting `$env:UNITY_PROJECT_PATH` and confirming no other
Unity Editor process is using that project:

```powershell
$results = Join-Path $env:TEMP 'summit-playmode-results.xml'
$log = Join-Path $env:TEMP 'summit-playmode.log'
& $env:UNITY_EDITOR_PATH -batchmode -runTests `
  -projectPath $env:UNITY_PROJECT_PATH `
  -testPlatform PlayMode `
  -assemblyNames CollectibleCoursePlayMode_ASummitCrystalTrialIn3DThirdPerson `
  -testResults $results -logFile $log
```

To run the existing Urban Survival multi-wave regression, substitute this
assembly name:

```text
T43MultiWaveValidation
```

Use the Unity process exit code and the resulting XML to determine the actual
test outcome. The recorded Phase 3 results are summarized in
`PHASE3_PROGRESS.md`; detailed execution logs from that run were retained under
the developer's `%TEMP%` directory and are not committed.

## Safety and current limitations

- Unity writes are restricted by the construction bridge to approved generated
  asset paths and allowlisted operations. Scene/prefab replacement is refused
  by default; explicitly approved replacement paths preserve backups.
- Do not run Git commands from a parent workspace/repository. This project
  repository is rooted here.
- The generated course uses primitive geometry and placeholder materials.
- Automated movement validation injects a deterministic movement vector; the
  recorded test is not a manual keyboard-input test. The developer separately
  confirmed opening the generated scene in Unity and that it worked; no claim
  is made that every feature was manually tested.
- The registry is broader than the currently implemented construction
  executors. Add and test an executor before relying on an unsupported
  capability.
- RAG, multi-agent orchestration, fine-tuning, and reinforcement learning are
  not implemented or claimed.

## Validation snapshot

See `PHASE3_PROGRESS.md` for the full status and exact report locations.
Recorded results include 28 passing Python unit tests, 3/3 generated-game
PlayMode tests, 1/1 T4.3 regression test with all 112 named runtime-check lines
passing, a successful bounded Phase 2 recovery, successful Unity compilation,
and a SHA-256 check showing no changes to the 39 protected production files.
