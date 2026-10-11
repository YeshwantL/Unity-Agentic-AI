using UnityEngine;

// Basic third-person movement using Unity's CharacterController.
[RequireComponent(typeof(CharacterController))]
public class PlayerController : MonoBehaviour
{
    [SerializeField] private Transform cameraTransform;
    [SerializeField] private float movementSpeed = 5f;
    [SerializeField] private float rotationSpeed = 12f;
    [SerializeField] private float gravity = -9.81f;

    private CharacterController characterController;
    private float verticalVelocity;

    private void Awake()
    {
        characterController = GetComponent<CharacterController>();
        if (cameraTransform == null && Camera.main != null)
            cameraTransform = Camera.main.transform;
    }

    private void Update()
    {
        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        // Read WASD (or the configured Unity horizontal/vertical axes).
        Vector2 input = new Vector2(Input.GetAxisRaw("Horizontal"), Input.GetAxisRaw("Vertical"));
        input = Vector2.ClampMagnitude(input, 1f);

        Vector3 forward = cameraTransform != null ? cameraTransform.forward : transform.forward;
        Vector3 right = cameraTransform != null ? cameraTransform.right : transform.right;
        forward.y = 0f;
        right.y = 0f;
        forward.Normalize();
        right.Normalize();

        Vector3 moveDirection = (forward * input.y + right * input.x).normalized;
        characterController.Move(moveDirection * movementSpeed * Time.deltaTime);

        // Turn toward the direction of travel, leaving the player still when idle.
        if (moveDirection.sqrMagnitude > 0.001f)
        {
            Quaternion targetRotation = Quaternion.LookRotation(moveDirection);
            transform.rotation = Quaternion.Slerp(transform.rotation, targetRotation,
                rotationSpeed * Time.deltaTime);
        }

        if (characterController.isGrounded && verticalVelocity < 0f)
            verticalVelocity = -2f;
        verticalVelocity += gravity * Time.deltaTime;
        characterController.Move(Vector3.up * verticalVelocity * Time.deltaTime);
    }
}
