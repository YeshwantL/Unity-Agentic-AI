namespace UnityAgent.Generated.ASummitCrystalTrialIn3DThirdPerson
{
using UnityEngine;

public sealed class CourseHud : MonoBehaviour
{
    [SerializeField] private CourseGameManager gameManager;
    public CourseGameManager GameManager => gameManager;

    public string CounterText => gameManager == null
        ? "Gems: 0/0"
        : "Gems: " + gameManager.CurrentCount + "/" + gameManager.RequiredCollectibles;

    public void BindConstruction(CourseGameManager manager)
    {
        gameManager = manager;
    }

    private void OnGUI()
    {
        if (gameManager == null)
            return;

        GUI.Box(new Rect(12f, 12f, 250f, 100f), "Collectible Course");
        GUI.Label(new Rect(24f, 38f, 220f, 22f), CounterText);
        string state = gameManager.State == CourseGameManager.RunState.Complete
            ? "Course complete!"
            : gameManager.State == CourseGameManager.RunState.Playing
                ? "WASD / arrows to move, Space to jump"
                : "Ready";
        GUI.Label(new Rect(24f, 60f, 220f, 22f), state);

        if (gameManager.State == CourseGameManager.RunState.Ready)
        {
            if (GUI.Button(new Rect(24f, 82f, 100f, 24f), "Start"))
                gameManager.StartGame();
        }
        else if (GUI.Button(new Rect(24f, 82f, 100f, 24f), "Restart"))
        {
            gameManager.Restart();
        }
    }
}
}
