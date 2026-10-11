using System;
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
