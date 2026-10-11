using System.Collections.Generic;
using UnityEngine;

public sealed class CourseGameManager : MonoBehaviour
{
    public enum RunState { Ready, Playing, Complete }

    [SerializeField] private GameObject player;
    [SerializeField, Min(1)] private int requiredCollectibles = 3;

    private readonly HashSet<CourseCollectible> collected = new HashSet<CourseCollectible>();
    private CourseCollectible[] allCollectibles = new CourseCollectible[0];
    private Vector3 initialPlayerPosition;
    private Quaternion initialPlayerRotation;

    public int RequiredCollectibles => requiredCollectibles;
    public int CurrentCount => collected.Count;
    public GameObject Player => player;
    public RunState State { get; private set; } = RunState.Ready;

    public void BindConstruction(GameObject playerObject, int requiredCount)
    {
        player = playerObject;
        requiredCollectibles = requiredCount;
    }

    private void Start()
    {
        if (player == null)
        {
            Debug.LogError("CourseGameManager requires a player reference.", this);
            return;
        }

        initialPlayerPosition = player.transform.position;
        initialPlayerRotation = player.transform.rotation;
        allCollectibles = FindObjectsByType<CourseCollectible>(FindObjectsSortMode.None);
        foreach (CourseCollectible collectible in allCollectibles)
            collectible.Bind(this);
    }

    public void StartGame()
    {
        if (State == RunState.Ready)
            State = RunState.Playing;
    }

    public bool TryCollect(CourseCollectible collectible)
    {
        if (State != RunState.Playing || collectible == null || !collected.Add(collectible))
            return false;

        collectible.MarkCollected();
        return true;
    }

    public bool TryFinish(GameObject entrant)
    {
        if (State != RunState.Playing || entrant != player
            || CurrentCount < requiredCollectibles)
            return false;

        State = RunState.Complete;
        return true;
    }

    public void Restart()
    {
        collected.Clear();
        foreach (CourseCollectible collectible in allCollectibles)
            collectible.ResetCollection();

        if (player != null)
        {
            CharacterController controller = player.GetComponent<CharacterController>();
            if (controller != null)
                controller.enabled = false;
            player.transform.SetPositionAndRotation(initialPlayerPosition, initialPlayerRotation);
            if (controller != null)
                controller.enabled = true;
        }

        State = RunState.Ready;
    }
}
