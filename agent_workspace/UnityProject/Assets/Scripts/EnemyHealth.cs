using System;
using UnityEngine;
using UnityEngine.Events;

// Health component for an enemy; uses the IDamageable interface already defined by PlayerWeapon.cs.
public class EnemyHealth : MonoBehaviour, IDamageable
{
    [SerializeField, Min(1f)] private float maxHealth = 50f;
    [SerializeField] private UnityEvent onDeath;

    public float CurrentHealth { get; private set; }
    public bool IsDead { get; private set; }
    public event Action<EnemyHealth> Died;

    private void Awake()
    {
        CurrentHealth = maxHealth;
        IsDead = false;
    }

    public void TakeDamage(float amount)
    {
        if (IsDead || amount <= 0f)
            return;

        CurrentHealth = Mathf.Max(0f, CurrentHealth - amount);
        if (CurrentHealth <= 0f)
            Die();
    }

    private void Die()
    {
        // Set the state before notifying listeners so death is handled only once.
        if (IsDead)
            return;

        IsDead = true;
        foreach (Collider enemyCollider in GetComponentsInChildren<Collider>())
            enemyCollider.enabled = false;

        Died?.Invoke(this);
        onDeath?.Invoke();
    }
}
