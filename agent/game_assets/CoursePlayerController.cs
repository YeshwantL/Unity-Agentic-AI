using UnityEngine;
using UnityEngine.InputSystem;

[RequireComponent(typeof(CharacterController))]
public sealed class CoursePlayerController : MonoBehaviour
{
    [SerializeField] private CourseGameManager gameManager;
    [SerializeField] private Transform cameraTransform;
    [SerializeField, Min(0.1f)] private float moveSpeed = 6f;
    [SerializeField, Min(0.1f)] private float jumpHeight = 1.4f;

    private CharacterController controller;
    private float verticalVelocity;

    public CourseGameManager GameManager => gameManager;
    public Transform CameraTransform => cameraTransform;

    public void BindConstruction(
        CourseGameManager manager, Transform camera, float speed, float jump)
    {
        gameManager = manager;
        cameraTransform = camera;
        moveSpeed = speed;
        jumpHeight = jump;
    }

    private void Awake()
    {
        controller = GetComponent<CharacterController>();
    }

    private void Update()
    {
        if (gameManager == null || gameManager.State != CourseGameManager.RunState.Playing)
            return;

        Vector2 input = Vector2.zero;
        Keyboard keyboard = Keyboard.current;
        if (keyboard != null)
        {
            input.x = (keyboard.dKey.isPressed || keyboard.rightArrowKey.isPressed ? 1f : 0f)
                - (keyboard.aKey.isPressed || keyboard.leftArrowKey.isPressed ? 1f : 0f);
            input.y = (keyboard.wKey.isPressed || keyboard.upArrowKey.isPressed ? 1f : 0f)
                - (keyboard.sKey.isPressed || keyboard.downArrowKey.isPressed ? 1f : 0f);
            if (keyboard.spaceKey.wasPressedThisFrame && controller.isGrounded)
                verticalVelocity = Mathf.Sqrt(jumpHeight * -2f * Physics.gravity.y);
        }

        ApplyMovement(input, Time.deltaTime);
    }

    public void ApplyMovement(Vector2 input, float deltaTime)
    {
        if (gameManager == null || gameManager.State != CourseGameManager.RunState.Playing
            || controller == null || !controller.enabled
            || !(deltaTime > 0f) || float.IsInfinity(deltaTime))
            return;

        Vector3 forward = cameraTransform != null ? cameraTransform.forward : Vector3.forward;
        Vector3 right = cameraTransform != null ? cameraTransform.right : Vector3.right;
        forward.y = 0f;
        right.y = 0f;
        Vector3 direction = Vector3.ClampMagnitude(
            forward.normalized * input.y + right.normalized * input.x, 1f);

        if (direction.sqrMagnitude > 0.001f)
            transform.rotation = Quaternion.Slerp(
                transform.rotation, Quaternion.LookRotation(direction), 12f * deltaTime);

        if (controller.isGrounded && verticalVelocity < 0f)
            verticalVelocity = -2f;
        verticalVelocity += Physics.gravity.y * deltaTime;
        controller.Move((direction * moveSpeed + Vector3.up * verticalVelocity) * deltaTime);
    }
}
