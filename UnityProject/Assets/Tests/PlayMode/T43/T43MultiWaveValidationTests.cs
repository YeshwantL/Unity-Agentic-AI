using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public class T43MultiWaveValidationTests
{
    private const string Prefix = "[T4.3 RUNTIME] ";
    private readonly List<string> failures = new List<string>();
    private readonly List<string> runtimeErrors = new List<string>();

    [UnityTest]
    public IEnumerator PlayAndClearThreeProgressiveWavesInSampleScene()
    {
        Application.logMessageReceived += CaptureRuntimeLog;
        if (SceneManager.GetActiveScene().name != "SampleScene")
        {
            SceneManager.LoadScene("SampleScene");
            yield return null;
        }

        Component state = null;
        Component wave = null;
        Component score = null;
        Component playerHealth = null;
        Component weapon = null;
        Camera aimCamera = null;
        Component followCamera = null;
        for (int i = 0; i < 300; i++)
        {
            state = GetManager();
            wave = FindFirst("WaveManager");
            score = FindFirst("ScoreManager");
            playerHealth = FindFirst("PlayerHealth");
            weapon = FindFirst("PlayerWeapon");
            aimCamera = Camera.main;
            followCamera = aimCamera != null ? aimCamera.GetComponent(FindType("ThirdPersonCamera")) : null;
            if (state != null && wave != null && score != null && playerHealth != null
                && weapon != null && aimCamera != null && FindFirst("GameHUD") != null
                && Count("GameStateManager") == 1)
                break;
            yield return null;
        }

        Check("SampleScene loaded", SceneManager.GetActiveScene().name == "SampleScene");
        Check("One production Player", Count("PlayerHealth") == 1 && Count("PlayerWeapon") == 1
            && GameObject.FindGameObjectsWithTag("Player").Length == 1);
        Check("One production WaveManager", Count("WaveManager") == 1);
        Check("One production GameStateManager", Count("GameStateManager") == 1);
        Check("One production ScoreManager", Count("ScoreManager") == 1);
        Check("Production HUD initialized", Count("GameHUD") == 1);
        Check("Production components initialized", state != null && wave != null && score != null
            && playerHealth != null && weapon != null && aimCamera != null);

        Transform[] configuredSpawns = wave != null
            ? (Transform[])GetPrivateField(wave, "spawnPoints") : Array.Empty<Transform>();
        GameObject assignedEnemyPrefab = wave != null ? (GameObject)GetPrivateField(wave, "enemyPrefab") : null;
        Component prefabHealth = assignedEnemyPrefab != null
            ? assignedEnemyPrefab.GetComponent(FindType("EnemyHealth")) : null;
        Component prefabController = assignedEnemyPrefab != null
            ? assignedEnemyPrefab.GetComponent(FindType("EnemyController")) : null;
        Check("Production player and camera references are configured",
            playerHealth != null && playerHealth.GetComponent<CharacterController>() != null
            && playerHealth.GetComponent(FindType("PlayerController")) != null
            && weapon != null && (Camera)GetPrivateField(weapon, "aimCamera") == aimCamera
            && followCamera != null && (Transform)GetPrivateField(followCamera, "target") == playerHealth.transform);
        Check("WaveManager uses the production prefab and four spawn points",
            prefabHealth != null && prefabController != null
            && assignedEnemyPrefab.GetComponent<Collider>() != null && configuredSpawns.Length == 4
            && configuredSpawns.All(point => point != null));
        Check("ScoreManager is the configured production score object",
            (Component)GetPrivateField(wave, "scoreManager") == score);
        Assert.That(failures, Is.Empty, "SampleScene is missing required production components: " + JoinFailures());

        int startingEnemies = GetPrivateInt(wave, "startingEnemies");
        int additionalEnemies = GetPrivateInt(wave, "additionalEnemiesPerWave");
        int pointsPerEnemy = GetPrivateInt(wave, "pointsPerEnemy");
        int configuredMaximum = GetPrivateInt(wave, "maximumWaves");
        float spawnInterval = GetPrivateFloat(wave, "spawnInterval");
        float timeBetweenWaves = GetPrivateFloat(wave, "timeBetweenWaves");
        float playerMaxHealth = GetPropertyFloat(playerHealth, "MaxHealth");
        float enemyMaxHealth = prefabHealth != null ? GetPrivateFloat(prefabHealth, "maxHealth") : -1f;
        float weaponDamage = GetPrivateFloat(weapon, "damage");
        int magazineSize = GetPropertyInt(weapon, "MagazineSize");
        float reloadTime = GetPrivateFloat(weapon, "reloadTime");
        float enemyDamage = prefabController != null ? GetPrivateFloat(prefabController, "attackDamage") : -1f;

        Debug.Log(Prefix + "CONFIG: startingEnemies=" + startingEnemies
            + "; additionalEnemiesPerWave=" + additionalEnemies
            + "; spawnInterval=" + spawnInterval.ToString("0.00")
            + "; timeBetweenWaves=" + timeBetweenWaves.ToString("0.00")
            + "; maximumWaves=" + configuredMaximum + "; enemyHealth=" + enemyMaxHealth
            + "; enemyDamage=" + enemyDamage + "; playerHealth=" + playerMaxHealth
            + "; weaponDamage=" + weaponDamage + "; magazine=" + magazineSize
            + "; reload=" + reloadTime.ToString("0.00") + "; scorePerEnemy=" + pointsPerEnemy);

        Check("Configured waves have increasing enemy counts", startingEnemies > 0
            && additionalEnemies > 0 && startingEnemies < startingEnemies + additionalEnemies
            && startingEnemies + additionalEnemies < startingEnemies + 2 * additionalEnemies);
        Check("Weapon and score configuration are usable", weaponDamage > 0f && enemyMaxHealth > 0f
            && magazineSize > 0 && pointsPerEnemy > 0 && enemyDamage > 0f && playerMaxHealth > 0f);
        Assert.That(failures, Is.Empty, "Current production configuration cannot satisfy the wave test: " + JoinFailures());

        if (followCamera != null)
            ((Behaviour)followCamera).enabled = false;

        List<int> clearedWaves = new List<int>();
        Action<int> clearedHandler = n => clearedWaves.Add(n);
        wave.GetType().GetEvent("WaveCleared").AddEventHandler(wave, clearedHandler);
        List<int> actualCounts = new List<int>();
        List<int> killsPerWave = new List<int>();
        List<int> scoreGains = new List<int>();
        List<float> clearSeconds = new List<float>();
        List<int> cumulativeScores = new List<int>();
        List<float> playerHealthAfterWaves = new List<float>();
        int priorScore = GetPropertyInt(score, "CurrentScore");
        HashSet<int> priorEnemyIds = new HashSet<int>();

        Invoke(state, "StartGame");
        Invoke(wave, "BeginWaves");
        Invoke(wave, "BeginWaves");
        Check("Game starts successfully", State(state) == "Playing");
        Check("Wave 1 starts", GetPropertyInt(wave, "CurrentWave") == 1);

        for (int waveNumber = 1; waveNumber <= 3; waveNumber++)
        {
            int expected = startingEnemies + (waveNumber - 1) * additionalEnemies;
            float waveStarted = Time.realtimeSinceStartup;
            float spawnDeadline = Time.realtimeSinceStartup + Mathf.Max(10f, expected * spawnInterval + 5f);
            HashSet<int> waveEnemyIds = new HashSet<int>();
            while (GetPropertyInt(wave, "ActiveEnemyCount") < expected && Time.realtimeSinceStartup < spawnDeadline)
            {
                CollectSpawned(waveEnemyIds, priorEnemyIds);
                yield return null;
            }
            CollectSpawned(waveEnemyIds, priorEnemyIds);
            actualCounts.Add(waveEnemyIds.Count);
            Check("Wave " + waveNumber + " enemies spawn", GetPropertyInt(wave, "ActiveEnemyCount") == expected
                && waveEnemyIds.Count == expected, "expected=" + expected + "; actual=" + waveEnemyIds.Count);
            if (waveNumber == 1)
            {
                Component firstController = FindAll("EnemyController")
                    .FirstOrDefault(controller => controller.gameObject.name.StartsWith("Enemy(Clone)", StringComparison.Ordinal));
                Check("Spawned EnemyController targets tagged Player", firstController != null
                    && (Transform)GetPrivateField(firstController, "playerTarget") == playerHealth.transform);
            }

            int kills = 0;
            float killDeadline = Time.realtimeSinceStartup + 90f;
            while ((GetPropertyInt(wave, "ActiveEnemyCount") > 0 || waveEnemyIds.Any(IsAliveEnemy))
                && State(state) == "Playing"
                && Time.realtimeSinceStartup < killDeadline)
            {
                Component target = FindAliveEnemy(waveEnemyIds, playerHealth.transform.position);
                if (target == null)
                {
                    CollectSpawned(waveEnemyIds, priorEnemyIds);
                    yield return null;
                    continue;
                }

                if (GetPropertyInt(weapon, "CurrentAmmo") == 0)
                {
                    Invoke(weapon, "Reload");
                    float reloadDeadline = Time.realtimeSinceStartup + reloadTime + 2f;
                    while ((GetPropertyBool(weapon, "IsReloading") || GetPropertyInt(weapon, "CurrentAmmo") == 0)
                        && Time.realtimeSinceStartup < reloadDeadline)
                        yield return null;
                    Check("Production reload completes", GetPropertyInt(weapon, "CurrentAmmo") == magazineSize
                        && !GetPropertyBool(weapon, "IsReloading"));
                }

                int beforeScore = GetPropertyInt(score, "CurrentScore");
                float beforeHealth = GetPropertyFloat(target, "CurrentHealth");
                AimAtEnemy(aimCamera, playerHealth.transform, target);
                yield return null;
                AimAtEnemy(aimCamera, playerHealth.transform, target);
                Physics.SyncTransforms();
                bool rayHit = Physics.Raycast(aimCamera.transform.position, aimCamera.transform.forward,
                    out RaycastHit rayHitInfo, 100f);
                string rayHitName = rayHit ? rayHitInfo.collider.name : "<none>";
                Component rayHitHealth = rayHit ? rayHitInfo.collider.GetComponent(FindType("EnemyHealth")) : null;
                int rayHitId = rayHitHealth != null ? rayHitHealth.GetInstanceID() : 0;
                Invoke(weapon, "Fire");
                yield return new WaitForSecondsRealtime(0.22f);

                bool targetDead = target == null || GetPropertyBool(target, "IsDead");
                bool targetDamaged = targetDead || GetPropertyFloat(target, "CurrentHealth") < beforeHealth;
                Check("PlayerWeapon damages real enemy", targetDamaged,
                    "wave=" + waveNumber + "; ammo=" + GetPropertyInt(weapon, "CurrentAmmo")
                    + "; target=" + target.GetInstanceID() + "; health=" + beforeHealth.ToString("0.0")
                    + "->" + (target != null ? GetPropertyFloat(target, "CurrentHealth").ToString("0.0") : "destroyed")
                    + "; rayHit=" + rayHitName + "; rayEnemyId=" + rayHitId
                    + "; playerHealth=" + GetPropertyFloat(playerHealth, "CurrentHealth").ToString("0.0")
                    + "; state=" + State(state));

                if (GetPropertyInt(weapon, "CurrentAmmo") == 0)
                {
                    Invoke(weapon, "Reload");
                    float reloadDeadline = Time.realtimeSinceStartup + reloadTime + 2f;
                    while ((GetPropertyBool(weapon, "IsReloading") || GetPropertyInt(weapon, "CurrentAmmo") == 0)
                        && Time.realtimeSinceStartup < reloadDeadline)
                        yield return null;
                    Check("Production reload completes", GetPropertyInt(weapon, "CurrentAmmo") == magazineSize
                        && !GetPropertyBool(weapon, "IsReloading"));
                }

                if (target != null && GetPropertyBool(target, "IsDead"))
                {
                    kills++;
                    Collider[] remainingColliders = target.GetComponentsInChildren<Collider>();
                    Check("Dead enemy collider no longer blocks shots", remainingColliders.Length > 0
                        && remainingColliders.All(collider => !collider.enabled));
                    Check("Enemy death awards exactly one configured score",
                        GetPropertyInt(score, "CurrentScore") == beforeScore + pointsPerEnemy,
                        "wave=" + waveNumber + "; gained=" + (GetPropertyInt(score, "CurrentScore") - beforeScore)
                        + "; expected=" + pointsPerEnemy);
                    int deadScore = GetPropertyInt(score, "CurrentScore");
                    Invoke(target, "TakeDamage", weaponDamage);
                    yield return null;
                    Check("Repeated damage does not award duplicate score", GetPropertyInt(score, "CurrentScore") == deadScore);
                }
                yield return null;
            }

            float clearDeadline = Time.realtimeSinceStartup + 10f;
            while (clearedWaves.Count < waveNumber && Time.realtimeSinceStartup < clearDeadline)
                yield return null;

            float duration = Time.realtimeSinceStartup - waveStarted;
            clearSeconds.Add(duration);
            killsPerWave.Add(kills);
            int gain = GetPropertyInt(score, "CurrentScore") - priorScore;
            scoreGains.Add(gain);
            priorScore = GetPropertyInt(score, "CurrentScore");
            cumulativeScores.Add(priorScore);
            playerHealthAfterWaves.Add(GetPropertyFloat(playerHealth, "CurrentHealth"));
            priorEnemyIds.UnionWith(waveEnemyIds);
            Check("Wave " + waveNumber + " clears", clearedWaves.Count == waveNumber
                && clearedWaves[waveNumber - 1] == waveNumber && GetPropertyInt(wave, "ActiveEnemyCount") == 0,
                "kills=" + kills + "; clearSeconds=" + duration.ToString("0.00"));
            Check("Wave " + waveNumber + " score matches kills", gain == kills * pointsPerEnemy,
                "gained=" + gain + "; kills=" + kills + "; pointsPerEnemy=" + pointsPerEnemy);
            Check("Game remains Playing after wave " + waveNumber, State(state) == "Playing");

            if (waveNumber < 3)
            {
                float nextWaveDeadline = Time.realtimeSinceStartup + Mathf.Max(10f, timeBetweenWaves + 5f);
                while (GetPropertyInt(wave, "CurrentWave") < waveNumber + 1
                    && Time.realtimeSinceStartup < nextWaveDeadline)
                    yield return null;
                Check("Wave " + (waveNumber + 1) + " begins", GetPropertyInt(wave, "CurrentWave") == waveNumber + 1);
            }
        }

        int expectedTotal = Enumerable.Range(1, 3).Sum(n => (startingEnemies + (n - 1) * additionalEnemies) * pointsPerEnemy);
        Check("Controlled difficulty increase", actualCounts.Count == 3
            && actualCounts[0] < actualCounts[1] && actualCounts[1] < actualCounts[2],
            "counts=" + string.Join(",", actualCounts));
        Check("No duplicate wave execution", clearedWaves.SequenceEqual(new[] { 1, 2, 3 })
            && GetPropertyInt(wave, "CurrentWave") == 3 && GetPropertyInt(wave, "ActiveEnemyCount") == 0,
            "cleared=" + string.Join(",", clearedWaves));
        Check("No duplicate score awards", GetPropertyInt(score, "CurrentScore") == expectedTotal,
            "score=" + GetPropertyInt(score, "CurrentScore") + "; expected=" + expectedTotal);
        Check("No duplicate managers", Count("WaveManager") == 1 && Count("ScoreManager") == 1
            && Count("GameStateManager") == 1);
        yield return new WaitForSecondsRealtime(0.35f);
        Check("HUD reports current wave and score", Count("GameHUD") == 1 && HudMatches(3, expectedTotal));
        Check("No NullReferenceException or unexpected runtime errors", runtimeErrors.Count == 0,
            string.Join(" | ", runtimeErrors));

        Debug.Log(Prefix + "MEASUREMENTS: expected=" + string.Join(",", Enumerable.Range(1, 3)
            .Select(n => startingEnemies + (n - 1) * additionalEnemies))
            + "; actual=" + string.Join(",", actualCounts)
            + "; kills=" + string.Join(",", killsPerWave)
            + "; score=" + string.Join(",", scoreGains)
            + "; cumulative=" + string.Join(",", cumulativeScores)
            + "; playerHealth=" + string.Join(",", playerHealthAfterWaves.Select(x => x.ToString("0.0")))
            + "; clearSeconds=" + string.Join(",", clearSeconds.Select(x => x.ToString("0.00")))
            + "; nextWaveStartDelay=" + timeBetweenWaves.ToString("0.00")
            + "; maximumWaves=" + configuredMaximum);

        wave.GetType().GetEvent("WaveCleared").RemoveEventHandler(wave, clearedHandler);
        if (followCamera != null)
            ((Behaviour)followCamera).enabled = true;
        Application.logMessageReceived -= CaptureRuntimeLog;
        Assert.That(failures, Is.Empty, "T4.3 runtime validation failures: " + JoinFailures());
    }

    private void CollectSpawned(HashSet<int> ids, HashSet<int> excludedIds)
    {
        foreach (Component enemy in FindAll("EnemyHealth"))
            if (enemy != null && !excludedIds.Contains(enemy.GetInstanceID())
                && enemy.gameObject.name.StartsWith("Enemy(Clone)", StringComparison.Ordinal))
                ids.Add(enemy.GetInstanceID());
    }

    private static bool IsAliveEnemy(int id)
    {
        return FindAll("EnemyHealth").Any(e => e != null && e.GetInstanceID() == id && !GetPropertyBool(e, "IsDead"));
    }

    private static Component FindAliveEnemy(HashSet<int> ids, Vector3 playerPosition)
    {
        return FindAll("EnemyHealth").Where(e => e != null && ids.Contains(e.GetInstanceID())
            && !GetPropertyBool(e, "IsDead"))
            .OrderBy(e => (e.transform.position - playerPosition).sqrMagnitude).FirstOrDefault();
    }

    private static void AimAtEnemy(Camera camera, Transform player, Component enemy)
    {
        Vector3 target = enemy.transform.position + Vector3.up * 0.6f;
        Vector3 origin = enemy.transform.position + Vector3.up * 3f;
        camera.transform.SetPositionAndRotation(origin, Quaternion.LookRotation(target - origin));
    }

    private static bool HudMatches(int wave, int score)
    {
        Component hud = FindFirst("GameHUD");
        if (hud == null) return false;
        TextValue(hud, "waveText", out string waveValue);
        TextValue(hud, "scoreText", out string scoreValue);
        return waveValue == "Wave: " + wave && scoreValue == "Score: " + score;
    }

    private static void TextValue(object target, string fieldName, out string value)
    {
        object component = GetPrivateField(target, fieldName);
        value = component == null ? string.Empty : (string)component.GetType().GetProperty("text").GetValue(component);
    }

    private void CaptureRuntimeLog(string condition, string stackTrace, LogType type)
    {
        if (type == LogType.Exception || type == LogType.Error)
            runtimeErrors.Add(type + ": " + condition);
    }

    private void Check(string name, bool passed, string details = "")
    {
        Debug.Log(Prefix + name + ": " + (passed ? "PASS" : "FAIL")
            + (string.IsNullOrEmpty(details) ? "" : " - " + details));
        if (!passed) failures.Add(name);
    }

    private static Component GetManager() => FindFirst("GameStateManager");
    private static Component FindFirst(string name) => FindAll(name).FirstOrDefault();
    private static Component[] FindAll(string name) => UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
        .Where(component => component != null && component.GetType().Name == name).ToArray();
    private static int Count(string name) => FindAll(name).Length;
    private static Type FindType(string name) => AppDomain.CurrentDomain.GetAssemblies()
        .Select(assembly => assembly.GetType(name)).FirstOrDefault(type => type != null);
    private static object Invoke(object target, string method, params object[] args) => target.GetType()
        .GetMethod(method).Invoke(target, args);
    private static object GetProperty(object target, string property) => target.GetType()
        .GetProperty(property).GetValue(target);
    private static int GetPropertyInt(object target, string property) => Convert.ToInt32(GetProperty(target, property));
    private static float GetPropertyFloat(object target, string property) => Convert.ToSingle(GetProperty(target, property));
    private static bool GetPropertyBool(object target, string property) => Convert.ToBoolean(GetProperty(target, property));
    private static string State(object target) => GetProperty(target, "CurrentState").ToString();
    private static int GetPrivateInt(object target, string name) => Convert.ToInt32(GetPrivateField(target, name));
    private static float GetPrivateFloat(object target, string name) => Convert.ToSingle(GetPrivateField(target, name));
    private static object GetPrivateField(object target, string name) => target.GetType()
        .GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).GetValue(target);
    private string JoinFailures() => string.Join("; ", failures);
}
