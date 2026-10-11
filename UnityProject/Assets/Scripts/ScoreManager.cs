using System;
using UnityEngine;

// Owns the current run score and exposes an event for future HUD listeners.
public class ScoreManager : MonoBehaviour
{
    public int CurrentScore { get; private set; }
    public event Action<int> ScoreChanged;

    public void AwardPoints(int points)
    {
        if (points <= 0)
            return;

        CurrentScore += points;
        ScoreChanged?.Invoke(CurrentScore);
    }
}
