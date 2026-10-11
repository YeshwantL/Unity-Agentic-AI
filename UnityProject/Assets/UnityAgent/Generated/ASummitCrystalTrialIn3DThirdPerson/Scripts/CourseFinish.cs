namespace UnityAgent.Generated.ASummitCrystalTrialIn3DThirdPerson
{
using UnityEngine;

public sealed class CourseFinish : MonoBehaviour
{
    [SerializeField] private CourseGameManager gameManager;
    [SerializeField] private GameObject player;

    public CourseGameManager GameManager => gameManager;
    public GameObject Player => player;

    public void BindConstruction(CourseGameManager manager, GameObject playerObject)
    {
        gameManager = manager;
        player = playerObject;
    }

    public bool TryFinish(GameObject entrant)
    {
        return gameManager != null && gameManager.TryFinish(entrant);
    }

    private void OnTriggerEnter(Collider other)
    {
        if (other.gameObject == player && TryFinish(player))
            Debug.Log("Obstacle course completed.", this);
    }
}
}
