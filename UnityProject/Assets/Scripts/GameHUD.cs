using System.Collections;
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
