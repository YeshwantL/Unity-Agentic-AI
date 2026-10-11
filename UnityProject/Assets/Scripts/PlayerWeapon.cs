using System.Collections;
using UnityEngine;

// Generic damage contract for future targets; this file does not depend on an Enemy class.
public interface IDamageable
{
    void TakeDamage(float amount);
}

// Simple hitscan weapon with ammunition, a fire-rate limit, and timed reloading.
public class PlayerWeapon : MonoBehaviour
{
    [SerializeField] private Camera aimCamera;
    [SerializeField, Min(0f)] private float damage = 25f;
    [SerializeField, Min(0f)] private float range = 100f;
    [SerializeField, Min(0.01f)] private float fireRate = 5f;
    [SerializeField, Min(1)] private int magazineSize = 12;
    [SerializeField, Min(0f)] private float reloadTime = 1.5f;

    private int currentAmmo;
    private float nextFireTime;
    private bool isReloading;

    public int CurrentAmmo => currentAmmo;
    public int MagazineSize => magazineSize;
    public bool IsReloading => isReloading;

    private void Awake()
    {
        currentAmmo = magazineSize;
        if (aimCamera == null)
            aimCamera = Camera.main;
    }

    public void Fire()
    {
        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        if (isReloading || currentAmmo <= 0 || Time.time < nextFireTime)
            return;

        if (aimCamera == null)
            aimCamera = Camera.main;
        if (aimCamera == null)
            return;

        // A shot is consumed when fired, whether or not it hits a damageable target.
        currentAmmo--;
        nextFireTime = Time.time + 1f / Mathf.Max(fireRate, 0.01f);

        if (Physics.Raycast(aimCamera.transform.position, aimCamera.transform.forward,
            out RaycastHit hit, range))
        {
            // Targets can opt in by implementing IDamageable; no enemy type is required.
            if (hit.collider.TryGetComponent<IDamageable>(out IDamageable damageable))
                damageable.TakeDamage(damage);
        }
    }

    public void Reload()
    {
        if (GameStateManager.Instance != null && !GameStateManager.Instance.IsGameplayRunning)
            return;

        if (!isReloading && currentAmmo < magazineSize)
            StartCoroutine(ReloadRoutine());
    }

    private IEnumerator ReloadRoutine()
    {
        isReloading = true;
        yield return new WaitForSeconds(reloadTime);
        currentAmmo = magazineSize;
        isReloading = false;
    }
}
