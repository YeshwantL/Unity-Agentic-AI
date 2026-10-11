using UnityEngine;

public sealed class CourseCollectible : MonoBehaviour
{
    [SerializeField] private CourseGameManager gameManager;
    private bool collected;

    public bool IsCollected => collected;
    public CourseGameManager GameManager => gameManager;

    public void BindConstruction(CourseGameManager manager)
    {
        gameManager = manager;
    }

    public void Bind(CourseGameManager manager)
    {
        gameManager = manager;
    }

    private void OnTriggerEnter(Collider other)
    {
        TryCollect(other.gameObject);
    }

    public bool TryCollect(GameObject entrant)
    {
        return !collected && gameManager != null && entrant == gameManager.Player
            && gameManager.TryCollect(this);
    }

    public void MarkCollected()
    {
        collected = true;
        gameObject.SetActive(false);
    }

    public void ResetCollection()
    {
        collected = false;
        gameObject.SetActive(true);
    }
}
