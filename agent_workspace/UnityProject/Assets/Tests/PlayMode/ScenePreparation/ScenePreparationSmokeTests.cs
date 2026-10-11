using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public class ScenePreparationSmokeTests
{
    [UnityTest]
    public IEnumerator SampleSceneInitializesAndSpawnsAnEnemy()
    {
        List<string> failures = new List<string>();
        List<string> nullReferences = new List<string>();
        Application.LogCallback handler = (message, stack, type) =>
        {
            if ((message != null && message.Contains("NullReferenceException"))
                || (stack != null && stack.Contains("NullReferenceException")))
                nullReferences.Add(message);
        };
        Application.logMessageReceived += handler;

        // Force a fresh scene lifecycle while the log listener is active.
        SceneManager.LoadScene("SampleScene");

        Component playerController = null;
        Component waveManager = null;
        Component gameStateManager = null;
        Component scoreManager = null;
        Component hud = null;
        for (int frame = 0; frame < 300; frame++)
        {
            yield return null;
            playerController = FindComponent("PlayerController");
            waveManager = FindComponent("WaveManager");
            gameStateManager = FindComponent("GameStateManager");
            scoreManager = FindComponent("ScoreManager");
            hud = FindComponent("GameHUD");
            if (playerController != null && waveManager != null && gameStateManager != null
                && scoreManager != null && hud != null
                && Count("GameStateManager") == 1
                && StaticProperty("GameStateManager", "Instance") == gameStateManager)
                break;
        }
        gameStateManager = FindComponent("GameStateManager");

        GameObject player = playerController != null ? playerController.gameObject : null;
        Check(failures, "scene loads", SceneManager.GetActiveScene().name == "SampleScene");
        Check(failures, "exactly one player", Count("PlayerController") == 1
            && player != null && player.CompareTag("Player"));
        Check(failures, "exactly one WaveManager", Count("WaveManager") == 1);
        Check(failures, "exactly one GameStateManager", Count("GameStateManager") == 1);
        Check(failures, "exactly one ScoreManager", Count("ScoreManager") == 1);
        Check(failures, "HUD initializes once", Count("GameHUD") == 1 && HudInitialized(hud));
        Component playerHealth = player != null ? player.GetComponent("PlayerHealth") : null;
        Component playerWeapon = player != null ? player.GetComponent("PlayerWeapon") : null;
        Check(failures, "PlayerHealth initializes", playerHealth != null
            && GetFloat(playerHealth, "CurrentHealth") > 0f
            && GetFloat(playerHealth, "CurrentHealth") <= GetFloat(playerHealth, "MaxHealth"));
        Check(failures, "PlayerWeapon initializes", playerWeapon != null
            && GetInt(playerWeapon, "MagazineSize") > 0
            && GetInt(playerWeapon, "CurrentAmmo") == GetInt(playerWeapon, "MagazineSize"));
        Camera[] cameras = UnityEngine.Object.FindObjectsByType<Camera>(FindObjectsSortMode.None);
        Camera[] mainCameras = cameras.Where(camera => camera != null && camera.CompareTag("MainCamera")).ToArray();
        Check(failures, "Main Camera initializes", mainCameras.Length == 1
            && FindComponent("ThirdPersonCamera") != null
            && GetField(FindComponent("ThirdPersonCamera"), "target") == (player != null ? player.transform : null));
        Check(failures, "player and camera references", player != null
            && player.GetComponent<CharacterController>() != null
            && playerHealth != null
            && playerWeapon != null
            && GetField(playerController, "cameraTransform") is Transform
            && GetField(playerWeapon, "aimCamera") == mainCameras.FirstOrDefault());

        object managerInstance = StaticProperty("GameStateManager", "Instance");
        Check(failures, "GameStateManager binds player and waves", gameStateManager != null
            && managerInstance == gameStateManager
            && GetField(gameStateManager, "playerHealth") != null
            && GetField(gameStateManager, "waveManager") == waveManager);
        Check(failures, "WaveManager references prefab and spawn points", waveManager != null
            && GetField(waveManager, "enemyPrefab") is GameObject
            && GetField(waveManager, "scoreManager") == scoreManager
            && GetField(waveManager, "spawnPoints") is Transform[] points
            && points.Length > 0 && points.All(point => point != null));
        GameObject enemyPrefab = GetField(waveManager, "enemyPrefab") as GameObject;
        Check(failures, "enemy prefab has health, controller, and collider", enemyPrefab != null
            && enemyPrefab.GetComponent("EnemyHealth") != null
            && enemyPrefab.GetComponent("EnemyController") != null
            && enemyPrefab.GetComponent<Collider>() != null);

        if (managerInstance != null)
            Invoke(managerInstance, "StartGame");
        float deadline = Time.realtimeSinceStartup + 5f;
        while (waveManager != null && GetInt(waveManager, "ActiveEnemyCount") == 0
            && Time.realtimeSinceStartup < deadline)
            yield return null;

        Component spawnedEnemyHealth = FindSpawnedEnemyHealth();
        Check(failures, "WaveManager starts", managerInstance != null
            && GetProperty(managerInstance, "CurrentState").ToString() == "Playing"
            && waveManager != null
            && GetInt(waveManager, "CurrentWave") == 1);
        Check(failures, "at least one enemy spawns", waveManager != null
            && GetInt(waveManager, "ActiveEnemyCount") > 0
            && spawnedEnemyHealth != null);
        Check(failures, "spawned EnemyController finds Player by tag", spawnedEnemyHealth != null
            && spawnedEnemyHealth.GetComponent("EnemyController") != null
            && GetField(spawnedEnemyHealth.GetComponent("EnemyController"), "playerTarget") == player.transform);

        yield return new WaitForSecondsRealtime(0.4f);
        Check(failures, "no NullReferenceException during initialization", nullReferences.Count == 0,
            string.Join(" | ", nullReferences));
        Check(failures, "no duplicate manager or HUD instances after spawn",
            Count("PlayerController") == 1 && Count("WaveManager") == 1
            && Count("GameStateManager") == 1 && Count("ScoreManager") == 1
            && Count("GameHUD") == 1 && UnityEngine.Object.FindObjectsByType<Camera>(FindObjectsSortMode.None)
                .Count(camera => camera != null && camera.CompareTag("MainCamera")) == 1);
        Application.logMessageReceived -= handler;
        Time.timeScale = 1f;
        Assert.That(failures, Is.Empty, string.Join("; ", failures));
    }

    private static Component FindComponent(string typeName) =>
        UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .FirstOrDefault(item => item != null && item.GetType().Name == typeName);

    private static int Count(string typeName) =>
        UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .Count(item => item != null && item.GetType().Name == typeName);

    private static Component FindSpawnedEnemyHealth() =>
        UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .FirstOrDefault(item => item != null && item.GetType().Name == "EnemyHealth"
                && item.gameObject.name.StartsWith("Enemy(Clone)"));

    private static bool HudInitialized(Component hud) => hud != null
        && GetField(hud, "healthText") != null
        && GetField(hud, "ammoText") != null
        && GetField(hud, "waveText") != null
        && GetField(hud, "scoreText") != null;

    private static Type RuntimeType(string typeName) => AppDomain.CurrentDomain.GetAssemblies()
        .Select(assembly => assembly.GetType(typeName)).FirstOrDefault(type => type != null);

    private static object StaticProperty(string typeName, string propertyName)
    {
        Type type = RuntimeType(typeName);
        return type == null ? null : type.GetProperty(propertyName, BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
    }

    private static object GetProperty(object target, string propertyName) => target == null ? null
        : target.GetType().GetProperty(propertyName, BindingFlags.Public | BindingFlags.Instance)?.GetValue(target);

    private static object GetField(object target, string fieldName) => target == null ? null
        : target.GetType().GetField(fieldName, BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(target);

    private static int GetInt(object target, string propertyName) => Convert.ToInt32(GetProperty(target, propertyName));
    private static float GetFloat(object target, string propertyName) => Convert.ToSingle(GetProperty(target, propertyName));

    private static object Invoke(object target, string methodName) => target.GetType()
        .GetMethod(methodName, BindingFlags.Public | BindingFlags.Instance).Invoke(target, null);

    private static void Check(List<string> failures, string name, bool passed, string details = "")
    {
        Debug.Log("[SCENE PREP SMOKE] " + name + ": " + (passed ? "PASS" : "FAIL")
            + (string.IsNullOrEmpty(details) ? "" : " - " + details));
        if (!passed) failures.Add(name);
    }
}
