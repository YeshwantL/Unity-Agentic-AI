using System;
using UnityEngine;
using UnityEngine.Events;

// Owns the player's health and one-time death notification only.
public class PlayerHealth : MonoBehaviour
{
    [SerializeField, Min(1f)] private float maxHealth = 100f;
    [SerializeField] private UnityEvent onDeath;

    public float CurrentHealth { get; private set; }
    public float MaxHealth => maxHealth;
    public bool IsDead { get; private set; }
    public event Action Died;

    private void Awake()
    {
        // Each player starts with the configured maximum health.
        CurrentHealth = maxHealth;
        IsDead = false;
    }

    public void TakeDamage(float amount)
    {
        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        if (IsDead || amount <= 0f)
            return;

        CurrentHealth = Mathf.Max(0f, CurrentHealth - amount);
        if (CurrentHealth <= 0f)
            Die();
    }

    private void Die()
    {
        // Guard against duplicate death handling if damage is applied again.
        if (IsDead)
            return;

        IsDead = true;
        Died?.Invoke();
        onDeath?.Invoke();
    }
}
