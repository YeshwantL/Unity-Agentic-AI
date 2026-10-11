using System;
using System.Collections;
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
    [SerializeField, Min(1)] private int pointsPerEnemy = 100;
    [SerializeField] private ScoreManager scoreManager;

    private readonly List<EnemyHealth> activeEnemies = new List<EnemyHealth>();
    private readonly HashSet<EnemyHealth> scoredEnemies = new HashSet<EnemyHealth>();
    private bool spawnFailed;
    private bool wavesStarted;

    public int CurrentWave { get; private set; }
    public event Action<int> WaveCleared;
    public int ActiveEnemyCount => activeEnemies.Count;

    private void Awake()
    {
        if (scoreManager == null)
            scoreManager = GetComponent<ScoreManager>();
        if (scoreManager == null)
            scoreManager = gameObject.AddComponent<ScoreManager>();
    }

    public void BeginWaves()
    {
        if (wavesStarted)
            return;

        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        // Do not start wave spawning until the central manager enters Playing.
        if (enemyPrefab == null)
        {
            Debug.LogError("WaveManager requires an enemy prefab reference.", this);
            return;
        }

        wavesStarted = true;
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
            WaveCleared?.Invoke(CurrentWave);
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
                health.Died += HandleEnemyDied;
            }

            if (spawnInterval > 0f && i < enemyCount - 1)
                yield return new WaitForSeconds(spawnInterval);
        }
    }

    private void HandleEnemyDied(EnemyHealth enemy)
    {
        if (enemy == null || !scoredEnemies.Add(enemy))
            return;

        if (scoreManager != null)
            scoreManager.AwardPoints(pointsPerEnemy);
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
