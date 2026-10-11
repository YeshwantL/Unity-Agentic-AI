using UnityEngine;

// Smooth orbit camera that follows and looks toward a target Transform.
public class ThirdPersonCamera : MonoBehaviour
{
    [SerializeField] private Transform target;
    [SerializeField] private float distance = 5f;
    [SerializeField] private float height = 2f;
    [SerializeField] private float mouseSensitivity = 3f;
    [SerializeField] private float minimumPitch = -25f;
    [SerializeField] private float maximumPitch = 65f;
    [SerializeField] private float smoothSpeed = 12f;

    private float yaw;
    private float pitch = 15f;

    private void LateUpdate()
    {
        if (target == null)
            return;

        // Mouse movement changes the orbit angles; pitch is clamped to avoid flipping.
        yaw += Input.GetAxis("Mouse X") * mouseSensitivity;
        pitch -= Input.GetAxis("Mouse Y") * mouseSensitivity;
        pitch = Mathf.Clamp(pitch, minimumPitch, maximumPitch);

        Vector3 lookPoint = target.position + Vector3.up * height;
        Quaternion orbit = Quaternion.Euler(pitch, yaw, 0f);
        Vector3 desiredPosition = lookPoint - orbit * Vector3.forward * distance;

        // Smooth both the camera position and its look-at rotation.
        float blend = 1f - Mathf.Exp(-smoothSpeed * Time.deltaTime);
        transform.position = Vector3.Lerp(transform.position, desiredPosition, blend);
        Quaternion desiredRotation = Quaternion.LookRotation(lookPoint - transform.position);
        transform.rotation = Quaternion.Slerp(transform.rotation, desiredRotation, blend);
    }
}
