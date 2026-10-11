using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace ScenePreparation
{
    // Idempotent Editor-only assembly of the existing gameplay scripts into SampleScene.
    public static class PreparePlayableScene
    {
        private const string ScenePath = "Assets/Scenes/SampleScene.unity";
        private const string EnemyPrefabPath = "Assets/Prefabs/Enemy.prefab";

        public static void Run()
        {
            try
            {
                Assemble();
                Debug.Log("[SCENE PREP] Scene objects, references, prefab, and build entry: PASS");
                EditorApplication.Exit(0);
            }
            catch (Exception exception)
            {
                Debug.LogError("[SCENE PREP] FAIL: " + exception);
                EditorApplication.Exit(1);
            }
        }

        private static void Assemble()
        {
            if (AssetDatabase.LoadAssetAtPath<SceneAsset>(ScenePath) == null)
                throw new FileNotFoundException("Configured SampleScene is missing.", ScenePath);

            Scene scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            EnsureSceneEntry();
            EnsureArena(scene);

            PlayerController playerController = EnsureUnique<PlayerController>("Player", scene);
            GameObject player = playerController != null ? playerController.gameObject : GameObject.Find("Player");
            if (player == null)
            {
                player = new GameObject("Player");
                player.transform.position = Vector3.zero;
            }
            playerController = EnsureComponent<PlayerController>(player);
            player.name = "Player";
            SetTag(player, "Player");
            CharacterController characterController = player.GetComponent<CharacterController>();
            if (characterController == null)
                characterController = player.AddComponent<CharacterController>();
            characterController.height = 2f;
            characterController.radius = 0.45f;
            characterController.center = new Vector3(0f, 1f, 0f);
            characterController.skinWidth = 0.08f;
            EnsureComponent<PlayerHealth>(player);
            EnsureComponent<PlayerWeapon>(player);
            EnsurePlayerVisual(player.transform);

            Camera camera = EnsureMainCamera(scene);
            ThirdPersonCamera thirdPersonCamera = EnsureUnique<ThirdPersonCamera>("Main Camera", scene);
            if (thirdPersonCamera == null)
                thirdPersonCamera = camera.gameObject.AddComponent<ThirdPersonCamera>();
            if (thirdPersonCamera.gameObject != camera.gameObject)
                throw new InvalidOperationException("ThirdPersonCamera must be on the Main Camera object.");
            camera.gameObject.tag = "MainCamera";
            camera.transform.position = new Vector3(0f, 5f, -7f);
            SetReference(playerController, "cameraTransform", camera.transform);
            SetReference(thirdPersonCamera, "target", player.transform);
            SetReference(player.GetComponent<PlayerWeapon>(), "aimCamera", camera);

            GameObject enemyPrefab = CreateOrLoadEnemyPrefab();
            ValidateEnemyPrefab(enemyPrefab);

            WaveManager waveManager = EnsureUnique<WaveManager>("WaveManager", scene);
            if (waveManager == null)
            {
                GameObject waveObject = GameObject.Find("WaveManager");
                if (waveObject == null)
                    waveObject = new GameObject("WaveManager");
                waveManager = EnsureComponent<WaveManager>(waveObject);
            }

            ScoreManager[] sceneScores = SceneComponents<ScoreManager>(scene);
            if (sceneScores.Length > 1)
                throw new InvalidOperationException("SampleScene has duplicate ScoreManager components.");
            ScoreManager scoreManager = sceneScores.Length == 1
                ? sceneScores[0]
                : waveManager.GetComponent<ScoreManager>();
            if (scoreManager == null)
                scoreManager = waveManager.gameObject.AddComponent<ScoreManager>();
            SetReference(waveManager, "enemyPrefab", enemyPrefab);
            SetReference(waveManager, "scoreManager", scoreManager);
            Transform[] spawnPoints = EnsureSpawnPoints(scene);
            SetArrayReferences(waveManager, "spawnPoints", spawnPoints);

            GameStateManager stateManager = EnsureUnique<GameStateManager>("GameStateManager", scene);
            if (stateManager == null)
            {
                GameObject stateObject = GameObject.Find("GameStateManager");
                if (stateObject == null)
                    stateObject = new GameObject("GameStateManager");
                stateManager = EnsureComponent<GameStateManager>(stateObject);
            }

            GameHUD[] sceneHuds = SceneComponents<GameHUD>(scene);
            if (sceneHuds.Length > 1)
                throw new InvalidOperationException("SampleScene has duplicate GameHUD components.");

            EditorSceneManager.MarkSceneDirty(scene);
            if (!EditorSceneManager.SaveScene(scene))
                throw new InvalidOperationException("Unity did not save SampleScene.");
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
            VerifySavedScene(enemyPrefab);
        }

        private static void EnsureSceneEntry()
        {
            EditorBuildSettingsScene[] scenes = EditorBuildSettings.scenes;
            if (scenes.Any(item => item.enabled && item.path == ScenePath))
                return;
            scenes = scenes.Where(item => item.path != ScenePath).ToArray();
            Array.Resize(ref scenes, scenes.Length + 1);
            scenes[scenes.Length - 1] = new EditorBuildSettingsScene(ScenePath, true);
            EditorBuildSettings.scenes = scenes;
        }

        private static void EnsureArena(Scene scene)
        {
            GameObject floor = GameObject.Find("Arena Floor");
            if (floor == null)
            {
                floor = GameObject.CreatePrimitive(PrimitiveType.Plane);
                floor.name = "Arena Floor";
                floor.transform.localScale = new Vector3(3f, 1f, 3f);
                floor.transform.position = new Vector3(0f, -0.05f, 0f);
                SceneManager.MoveGameObjectToScene(floor, scene);
            }
        }

        private static void EnsurePlayerVisual(Transform player)
        {
            if (player.Find("Player Visual") != null)
                return;
            GameObject visual = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            visual.name = "Player Visual";
            Collider primitiveCollider = visual.GetComponent<Collider>();
            if (primitiveCollider != null)
                UnityEngine.Object.DestroyImmediate(primitiveCollider);
            visual.transform.SetParent(player, false);
            visual.transform.localPosition = Vector3.up;
        }

        private static Camera EnsureMainCamera(Scene scene)
        {
            Camera[] cameras = SceneComponents<Camera>(scene);
            Camera tagged = cameras.FirstOrDefault(item => item.CompareTag("MainCamera"));
            if (tagged != null)
                return tagged;
            if (cameras.Length == 1)
                return cameras[0];
            if (cameras.Length > 1)
                throw new InvalidOperationException("SampleScene has multiple untagged cameras; refusing to guess.");

            GameObject cameraObject = new GameObject("Main Camera");
            SceneManager.MoveGameObjectToScene(cameraObject, scene);
            return cameraObject.AddComponent<Camera>();
        }

        private static GameObject CreateOrLoadEnemyPrefab()
        {
            Directory.CreateDirectory(Path.Combine(Application.dataPath, "Prefabs"));
            AssetDatabase.Refresh();
            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(EnemyPrefabPath);
            if (prefab != null)
                return prefab;

            GameObject enemy = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            enemy.name = "Enemy";
            enemy.AddComponent<EnemyHealth>();
            enemy.AddComponent<EnemyController>();
            prefab = PrefabUtility.SaveAsPrefabAsset(enemy, EnemyPrefabPath);
            UnityEngine.Object.DestroyImmediate(enemy);
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
            if (prefab == null)
                throw new InvalidOperationException("Unity failed to create Assets/Prefabs/Enemy.prefab.");
            return prefab;
        }

        private static void ValidateEnemyPrefab(GameObject prefab)
        {
            if (prefab.GetComponent<EnemyHealth>() == null
                || prefab.GetComponent<EnemyController>() == null
                || prefab.GetComponent<Collider>() == null)
                throw new InvalidOperationException("Enemy prefab must contain EnemyHealth, EnemyController, and a collider on its root.");
            string controllerSource = File.ReadAllText(Path.Combine(Application.dataPath, "Scripts/EnemyController.cs"));
            if (!controllerSource.Contains("FindGameObjectWithTag(\"Player\")"))
                throw new InvalidOperationException("EnemyController runtime target lookup could not be verified.");
        }

        private static Transform[] EnsureSpawnPoints(Scene scene)
        {
            GameObject root = GameObject.Find("Enemy Spawn Points");
            if (root == null)
            {
                root = new GameObject("Enemy Spawn Points");
                SceneManager.MoveGameObjectToScene(root, scene);
            }
            Vector3[] positions =
            {
                new Vector3(-8f, 0f, -8f), new Vector3(8f, 0f, -8f),
                new Vector3(-8f, 0f, 8f), new Vector3(8f, 0f, 8f)
            };
            Transform[] points = new Transform[positions.Length];
            for (int i = 0; i < positions.Length; i++)
            {
                string pointName = "Spawn Point " + (i + 1);
                Transform point = root.transform.Find(pointName);
                if (point == null)
                {
                    GameObject pointObject = new GameObject(pointName);
                    pointObject.transform.SetParent(root.transform, false);
                    point = pointObject.transform;
                }
                point.position = positions[i];
                points[i] = point;
            }
            return points;
        }

        private static void VerifySavedScene(GameObject expectedPrefab)
        {
            Scene scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            PlayerController player = UniqueInScene<PlayerController>(scene);
            Camera camera = SceneComponents<Camera>(scene).Single(item => item.CompareTag("MainCamera"));
            ThirdPersonCamera orbit = UniqueInScene<ThirdPersonCamera>(scene);
            WaveManager waves = UniqueInScene<WaveManager>(scene);
            GameStateManager state = UniqueInScene<GameStateManager>(scene);
            ScoreManager score = UniqueInScene<ScoreManager>(scene);

            Require(player.GetComponent<CharacterController>() != null, "Player CharacterController");
            Require(player.GetComponent<PlayerHealth>() != null, "Player PlayerHealth");
            Require(player.GetComponent<PlayerWeapon>() != null, "Player PlayerWeapon");
            Require(player.CompareTag("Player"), "Player tag for EnemyController runtime lookup");
            Require(GetReference(player, "cameraTransform") == camera.transform, "PlayerController cameraTransform");
            Require(GetReference(player.GetComponent<PlayerWeapon>(), "aimCamera") == camera, "PlayerWeapon aimCamera");
            Require(GetReference(orbit, "target") == player.transform, "ThirdPersonCamera target");
            Require(GetReference(waves, "enemyPrefab") == expectedPrefab, "WaveManager enemyPrefab");
            Require(GetReference(waves, "scoreManager") == score, "WaveManager ScoreManager reference");
            UnityEngine.Object[] configuredSpawnPoints = GetArrayReferences(waves, "spawnPoints");
            Require(configuredSpawnPoints.Length >= 1 && configuredSpawnPoints.All(point => point != null),
                "WaveManager spawnPoints");
            Require(state != null, "GameStateManager scene instance");
            Require(SceneComponents<PlayerHealth>(scene).Length == 1
                && SceneComponents<PlayerWeapon>(scene).Length == 1, "single player health and weapon");
            Require(SceneComponents<WaveManager>(scene).Length == 1, "single WaveManager");
            Require(SceneComponents<ScoreManager>(scene).Length == 1, "single ScoreManager");
            Require(SceneComponents<GameStateManager>(scene).Length == 1, "single GameStateManager");
            Require(SceneComponents<ThirdPersonCamera>(scene).Length == 1, "single ThirdPersonCamera");
            Require(SceneComponents<GameHUD>(scene).Length <= 1, "no duplicate scene HUD");
            Require(GameObject.Find("Arena Floor") != null, "minimal arena floor");
        }

        private static T EnsureUnique<T>(string objectName, Scene scene) where T : Component
        {
            T[] components = SceneComponents<T>(scene);
            if (components.Length > 1)
                throw new InvalidOperationException("SampleScene contains duplicate " + typeof(T).Name + " components.");
            if (components.Length == 1)
                return components[0];
            return null;
        }

        private static T UniqueInScene<T>(Scene scene) where T : Component
        {
            T[] components = SceneComponents<T>(scene);
            if (components.Length != 1)
                throw new InvalidOperationException("Expected exactly one " + typeof(T).Name + ", found " + components.Length + ".");
            return components[0];
        }

        private static T[] SceneComponents<T>(Scene scene) where T : Component =>
            UnityEngine.Object.FindObjectsByType<T>(FindObjectsSortMode.None)
                .Where(item => item != null && item.gameObject.scene == scene).ToArray();

        private static T EnsureComponent<T>(GameObject target) where T : Component
        {
            T component = target.GetComponent<T>();
            return component != null ? component : target.AddComponent<T>();
        }

        private static void SetReference(UnityEngine.Object target, string propertyName, UnityEngine.Object value)
        {
            SerializedObject serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property == null || property.propertyType != SerializedPropertyType.ObjectReference)
                throw new InvalidOperationException(typeof(SerializedObject).Name + " has no object-reference field '" + propertyName
                    + "' on " + target.GetType().Name + ".");
            property.objectReferenceValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static UnityEngine.Object GetReference(UnityEngine.Object target, string propertyName)
        {
            SerializedProperty property = new SerializedObject(target).FindProperty(propertyName);
            if (property == null)
                throw new InvalidOperationException("Serialized field '" + propertyName + "' was not found on " + target.GetType().Name + ".");
            return property.objectReferenceValue;
        }

        private static void SetArrayReferences(UnityEngine.Object target, string propertyName, UnityEngine.Object[] values)
        {
            SerializedObject serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property == null || !property.isArray)
                throw new InvalidOperationException("Serialized array '" + propertyName + "' was not found on " + target.GetType().Name + ".");
            property.arraySize = values.Length;
            for (int i = 0; i < values.Length; i++)
                property.GetArrayElementAtIndex(i).objectReferenceValue = values[i];
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static UnityEngine.Object[] GetArrayReferences(UnityEngine.Object target, string propertyName)
        {
            SerializedProperty property = new SerializedObject(target).FindProperty(propertyName);
            if (property == null || !property.isArray)
                throw new InvalidOperationException("Serialized array '" + propertyName + "' was not found on " + target.GetType().Name + ".");
            return Enumerable.Range(0, property.arraySize)
                .Select(index => property.GetArrayElementAtIndex(index).objectReferenceValue).ToArray();
        }

        private static void SetTag(GameObject target, string tag)
        {
            try { target.tag = tag; }
            catch (UnityException exception)
            {
                throw new InvalidOperationException("Required Unity tag '" + tag + "' is unavailable.", exception);
            }
        }

        private static void Require(bool condition, string description)
        {
            if (!condition)
                throw new InvalidOperationException("Scene verification failed: " + description);
        }
    }
}
