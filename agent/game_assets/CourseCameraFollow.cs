using UnityEngine;

public sealed class CourseCameraFollow : MonoBehaviour
{
    [SerializeField] private Transform target;
    [SerializeField] private Vector3 offset = new Vector3(0f, 6f, -8f);
    [SerializeField, Min(0f)] private float smoothing = 8f;

    public Transform Target => target;

    public void BindConstruction(Transform followTarget)
    {
        target = followTarget;
    }

    private void LateUpdate()
    {
        if (target == null)
            return;

        Vector3 destination = target.position + offset;
        transform.position = Vector3.Lerp(
            transform.position, destination, smoothing * Time.deltaTime);
        transform.LookAt(target.position + Vector3.up * 1.1f);
    }
}
