using System;
using System.Collections;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
#if UNITY_EDITOR
using UnityEditor.SceneManagement;
#endif
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

public sealed class CollectibleCoursePlayModeTests
{
    private const string ScenePath = "Assets/UnityAgent/Generated/ASummitCrystalTrialIn3DThirdPerson/Scenes/Main.unity";

    [OneTimeSetUp]
    public void LogSuiteSetup()
    {
        Debug.Log("[COURSE_TEST] suite setup: assembly discovery confirmed.");
    }

    [SetUp]
    public void LogTestSetup()
    {
        Debug.Log("[COURSE_TEST] test setup entered.");
    }

    [TearDown]
    public void LogTestTeardown()
    {
        Debug.Log("[COURSE_TEST] test teardown entered.");
    }

    [OneTimeTearDown]
    public void LogSuiteTeardown()
    {
        Debug.Log("[COURSE_TEST] suite teardown entered.");
    }

    [UnityTest]
    public IEnumerator DedicatedSceneLoadsAndPlayerMoves()
    {
        Debug.Log("[COURSE_TEST] DedicatedSceneLoadsAndPlayerMoves entered.");
        yield return LoadCourse();
        Debug.Log("[COURSE_TEST] scene load coroutine returned.");
        GameObject player = GameObject.Find("Player");
        Debug.Log("[COURSE_TEST] player lookup completed: " + (player != null));
        Assert.IsNotNull(player, "The generated course scene must contain a player.");
        Assert.IsNotNull(GameObject.Find("Main Camera"));
        Assert.IsNotNull(GameObject.Find("Course HUD"));
        Assert.IsNotNull(GameObject.Find("Finish"));
        Assert.AreEqual(3, UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .Count(component => component != null
                && component.GetType().Name == "CourseCollectible"));

        object manager = GetComponent("Course Manager", "CourseGameManager");
        Debug.Log("[COURSE_TEST] manager lookup completed: " + (manager != null));
        Invoke(manager, "StartGame");
        Vector3 before = player.transform.position;
        Component movement = GetComponent("Player", "CoursePlayerController");
        Debug.Log("[COURSE_TEST] movement lookup completed: " + (movement != null));
        CharacterController characterController = player.GetComponent<CharacterController>();
        Rigidbody body = player.GetComponent<Rigidbody>();
        Vector2 movementInput = Vector2.up;
        float movementDeltaTime = 0.1f;
        Debug.Log(
            "[COURSE_TEST] movement before input=" + movementInput
            + " player=" + before
            + " state=" + GetProperty(manager, "State")
            + " controller.enabled=" + characterController.enabled
            + " controller.isGrounded=" + characterController.isGrounded
            + " controller.height=" + characterController.height
            + " controller.radius=" + characterController.radius
            + " rigidbody.present=" + (body != null)
            + (body == null ? "" : " rigidbody.isKinematic=" + body.isKinematic
                + " rigidbody.useGravity=" + body.useGravity)
            + " camera.forward="
            + ((Transform)GetProperty(movement, "CameraTransform")).forward
            + " deltaTime=" + movementDeltaTime);
        Invoke(movement, "ApplyMovement", movementInput, movementDeltaTime);
        Vector3 after = player.transform.position;
        Debug.Log(
            "[COURSE_TEST] movement after input=" + movementInput
            + " deltaTime=" + movementDeltaTime
            + " player=" + after
            + " state=" + GetProperty(manager, "State")
            + " controller.enabled=" + characterController.enabled
            + " controller.isGrounded=" + characterController.isGrounded
            + " rigidbody.present=" + (body != null)
            + (body == null ? "" : " rigidbody.isKinematic=" + body.isKinematic
                + " rigidbody.useGravity=" + body.useGravity));
        Assert.Greater(player.transform.position.z, before.z,
            "Applying forward input must move the player through the course.");

        Component cameraFollow = GetComponent("Main Camera", "CourseCameraFollow");
        Debug.Log("[COURSE_TEST] camera lookup completed: " + (cameraFollow != null));
        Assert.AreSame(player.transform, GetProperty(cameraFollow, "Target"));
        Debug.Log("[COURSE_TEST] movement test assertions completed.");
    }

    [UnityTest]
    public IEnumerator CollectionUpdatesHudOnceAndUnlocksFinishOnlyAtTarget()
    {
        yield return LoadCourse();
        object manager = GetComponent("Course Manager", "CourseGameManager");
        GameObject player = GameObject.Find("Player");
        Component finish = GetComponent("Finish", "CourseFinish");
        Invoke(manager, "StartGame");

        Assert.IsFalse((bool)Invoke(finish, "TryFinish", player),
            "The finish must remain locked before all collectibles are collected.");

        Component[] gems = FindCollectibles();
        Assert.AreEqual(3, gems.Length);
        Invoke(gems[0], "TryCollect", player);
        Assert.AreEqual(1, GetProperty(manager, "CurrentCount"));
        Invoke(gems[0], "TryCollect", player);
        Assert.AreEqual(1, GetProperty(manager, "CurrentCount"),
            "A collectible must award at most once.");

        Component hud = GetComponent("Course HUD", "CourseHud");
        Assert.AreEqual("Gems: 1/3", GetProperty(hud, "CounterText"));

        Invoke(gems[1], "TryCollect", player);
        Invoke(gems[2], "TryCollect", player);
        Assert.AreEqual(3, GetProperty(manager, "CurrentCount"));
        Assert.IsTrue((bool)Invoke(finish, "TryFinish", player));
        Assert.AreEqual("Complete", GetProperty(manager, "State").ToString());
    }

    [UnityTest]
    public IEnumerator RestartRestoresPlayerCollectiblesAndReadyState()
    {
        yield return LoadCourse();
        object manager = GetComponent("Course Manager", "CourseGameManager");
        GameObject player = GameObject.Find("Player");
        Vector3 spawn = player.transform.position;
        Invoke(manager, "StartGame");
        Component[] gems = FindCollectibles();
        Invoke(gems[0], "TryCollect", player);
        player.transform.position += Vector3.forward * 5f;

        Invoke(manager, "Restart");
        yield return null;

        Assert.AreEqual(0, GetProperty(manager, "CurrentCount"));
        Assert.AreEqual("Ready", GetProperty(manager, "State").ToString());
        Assert.Less(Vector3.Distance(spawn, player.transform.position), 0.01f);
        Assert.IsTrue(FindCollectibles().All(item => (bool)GetProperty(item, "IsCollected") == false));
    }

    private static IEnumerator LoadCourse()
    {
        Debug.Log("[COURSE_TEST] scene loading requested: " + ScenePath);
#if UNITY_EDITOR
        var scene = EditorSceneManager.LoadSceneInPlayMode(
            ScenePath, new LoadSceneParameters(LoadSceneMode.Single));
        Debug.Log("[COURSE_TEST] scene loading call returned; valid=" + scene.IsValid());
        Assert.IsTrue(scene.IsValid(), "The generated course scene must load in PlayMode.");
#else
        Assert.Fail("The course scene loader is available only in the Unity Editor.");
#endif
        Debug.Log("[COURSE_TEST] scene loader yielding one frame.");
        yield return null;
        Debug.Log("[COURSE_TEST] scene loader resumed.");
    }

    private static Component[] FindCollectibles()
    {
        return UnityEngine.Object.FindObjectsByType<MonoBehaviour>(FindObjectsSortMode.None)
            .Where(component => component != null
                && component.GetType().Name == "CourseCollectible")
            .Cast<Component>().ToArray();
    }

    private static Component GetComponent(string objectName, string typeName)
    {
        GameObject gameObject = GameObject.Find(objectName);
        Assert.IsNotNull(gameObject, "Missing scene object: " + objectName);
        Component component = gameObject.GetComponents<MonoBehaviour>()
            .FirstOrDefault(item => item != null
                && item.GetType().Name == typeName
                && item.GetType().Namespace != null
                && item.GetType().Namespace.StartsWith(
                    "UnityAgent.Generated.", StringComparison.Ordinal));
        Assert.IsNotNull(component, "Missing component " + typeName + " on " + objectName);
        return component;
    }

    private static object Invoke(object target, string method, params object[] args)
    {
        Assert.IsNotNull(target);
        Type[] parameterTypes = args.Select(argument => argument.GetType()).ToArray();
        MethodInfo info = target.GetType().GetMethod(
            method, BindingFlags.Public | BindingFlags.Instance,
            null, parameterTypes, null);
        Assert.IsNotNull(info, "Missing public method " + method);
        return info.Invoke(target, args);
    }

    private static object GetProperty(object target, string name)
    {
        Assert.IsNotNull(target);
        PropertyInfo property = target.GetType().GetProperty(
            name, BindingFlags.Public | BindingFlags.Instance);
        Assert.IsNotNull(property, "Missing public property " + name);
        return property.GetValue(target);
    }
}
