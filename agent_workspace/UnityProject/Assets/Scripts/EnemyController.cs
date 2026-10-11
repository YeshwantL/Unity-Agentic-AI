using UnityEngine;

// Pursues a player target and applies close-range damage through PlayerHealth.
[RequireComponent(typeof(EnemyHealth))]
public class EnemyController : MonoBehaviour
{
    [SerializeField] private Transform playerTarget;
    [SerializeField, Min(0f)] private float movementSpeed = 3f;
    [SerializeField, Min(0.1f)] private float attackRange = 1.5f;
    [SerializeField, Min(0f)] private float attackDamage = 10f;
    [SerializeField, Min(0.01f)] private float attackCooldown = 1.5f;

    private EnemyHealth enemyHealth;
    private PlayerHealth playerHealth;
    private float nextAttackTime;

    private void Awake()
    {
        enemyHealth = GetComponent<EnemyHealth>();
        FindPlayerTarget();
    }

    private void Update()
    {
        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        // A defeated enemy remains inert and cannot chase or attack again.
        if (enemyHealth == null || enemyHealth.IsDead || playerTarget == null)
            return;

        if (playerHealth == null)
            playerHealth = playerTarget.GetComponentInParent<PlayerHealth>();
        if (playerHealth == null || playerHealth.IsDead)
            return;

        Vector3 offset = playerTarget.position - transform.position;
        offset.y = 0f;
        float distance = offset.magnitude;

        if (distance > attackRange)
        {
            Vector3 direction = offset.normalized;
            transform.position += direction * movementSpeed * Time.deltaTime;
            if (direction.sqrMagnitude > 0.001f)
                transform.rotation = Quaternion.LookRotation(direction);
            return;
        }

        if (Time.time >= nextAttackTime)
        {
            playerHealth.TakeDamage(attackDamage);
            nextAttackTime = Time.time + Mathf.Max(attackCooldown, 0.01f);
        }
    }

    private void FindPlayerTarget()
    {
        if (playerTarget == null)
        {
            GameObject playerObject = GameObject.FindGameObjectWithTag("Player");
            if (playerObject != null)
                playerTarget = playerObject.transform;
        }

        if (playerTarget != null)
            playerHealth = playerTarget.GetComponentInParent<PlayerHealth>();
    }
}
