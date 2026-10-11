using System;
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
