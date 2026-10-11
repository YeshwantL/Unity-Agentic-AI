using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace UnityAgent.Editor
{
    [Serializable]
    public sealed class ConstructionRequest
    {
        public string scene_path;
        public string prefab_path;
        public string prefab_object_name;
        public bool replace_existing;
        public ConstructionObject[] objects;
    }

    [Serializable]
    public sealed class ConstructionObject
    {
        public string name;
        public string primitive;
        public float[] position;
        public float[] scale;
        public float[] color;
        public ConstructionComponent[] components;
    }

    [Serializable]
    public sealed class ConstructionComponent
    {
        public string type;
        public bool has_is_trigger;
        public bool is_trigger;
        public bool has_radius;
        public float radius;
        public bool has_height;
        public float height;
        public bool has_mass;
        public float mass;
        public bool has_use_gravity;
        public bool use_gravity;
        public bool has_is_kinematic;
        public bool is_kinematic;
        public bool has_field_of_view;
        public float field_of_view;
        public bool has_intensity;
        public float intensity;
        public bool has_slope_limit;
        public float slope_limit;
        public bool has_step_offset;
        public float step_offset;
        public bool has_required_collectibles;
        public int required_collectibles;
        public bool has_move_speed;
        public float move_speed;
        public bool has_jump_height;
        public float jump_height;
        public string game_manager_name;
        public string player_name;
        public string camera_name;
        public string target_name;
    }

    [Serializable]
    public sealed class ConstructionFinding
    {
        public string object_name;
        public bool exists_after_reload;
        public string[] verified_components;
        public string error;
    }

    [Serializable]
    public sealed class ConstructionReport
    {
        public bool success;
        public string scene_path;
        public string prefab_path;
        public string[] created_asset_paths;
        public ConstructionFinding[] verification_findings;
        public string[] errors;
    }

    public static class UnityAgentConstruction
    {
        private const string AllowedAssetRoot = "Assets/UnityAgent/Generated/";
        private const string RequestEnvironmentVariable = "UNITY_AGENT_CONSTRUCTION_REQUEST";
        private const string ReportPrefix = "UNITY_AGENT_CONSTRUCTION_RESULT:";

        public static void Run()
        {
            ConstructionReport report;
            try
            {
                string requestPath = Environment.GetEnvironmentVariable(RequestEnvironmentVariable);
                if (string.IsNullOrWhiteSpace(requestPath) || !File.Exists(requestPath))
                    throw new InvalidOperationException("Construction request file is not available.");

                ConstructionRequest request = JsonUtility.FromJson<ConstructionRequest>(
                    File.ReadAllText(requestPath));
                report = BuildAndVerify(request);
            }
            catch (Exception exception)
            {
                report = new ConstructionReport
                {
                    success = false,
                    created_asset_paths = Array.Empty<string>(),
                    verification_findings = Array.Empty<ConstructionFinding>(),
                    errors = new[] { exception.Message }
                };
            }

            Debug.Log(ReportPrefix + JsonUtility.ToJson(report));
            EditorApplication.Exit(report.success ? 0 : 1);
        }

        private static ConstructionReport BuildAndVerify(ConstructionRequest request)
        {
            ValidateRequest(request);
            string sceneAbsolutePath = ResolveProjectAssetPath(request.scene_path);
            string sceneDirectory = Path.GetDirectoryName(sceneAbsolutePath);
            string gameplayNamespace = GetGameplayNamespace(request.scene_path);
            Directory.CreateDirectory(sceneDirectory);

            List<string> createdAssets = new List<string>();
            List<ConstructionFinding> findings = new List<ConstructionFinding>();
            Scene scene = EditorSceneManager.NewScene(
                NewSceneSetup.EmptyScene, NewSceneMode.Single);
            Dictionary<string, GameObject> objectsByName =
                new Dictionary<string, GameObject>(StringComparer.Ordinal);

            foreach (ConstructionObject item in request.objects)
            {
                GameObject gameObject = CreatePrimitive(item);
                SceneManager.MoveGameObjectToScene(gameObject, scene);
                objectsByName.Add(item.name, gameObject);
            }

            foreach (ConstructionObject item in request.objects)
            {
                GameObject gameObject = objectsByName[item.name];
                EnsureComponents(gameObject, item.components, gameplayNamespace);
            }

            foreach (ConstructionObject item in request.objects)
            {
                GameObject gameObject = objectsByName[item.name];
                ConfigureTransform(gameObject.transform, item);
                ConfigureMaterial(
                    gameObject, item, sceneDirectory,
                    Path.GetDirectoryName(request.scene_path).Replace('\\', '/'),
                    createdAssets);
                ConfigureComponents(
                    gameObject, item.components, objectsByName, gameplayNamespace);
            }

            if (!string.IsNullOrEmpty(request.prefab_path))
            {
                GameObject prefabSource = objectsByName[request.prefab_object_name];
                string prefabAbsolutePath = ResolveProjectAssetPath(request.prefab_path);
                Directory.CreateDirectory(Path.GetDirectoryName(prefabAbsolutePath));
                GameObject savedPrefab = PrefabUtility.SaveAsPrefabAsset(
                    prefabSource, prefabAbsolutePath);
                if (savedPrefab == null)
                    throw new InvalidOperationException(
                        "Unity did not create the requested prefab asset.");
                createdAssets.Add(request.prefab_path);
            }

            if (!EditorSceneManager.SaveScene(scene, request.scene_path))
                throw new InvalidOperationException("Unity did not save the requested scene.");
            createdAssets.Add(request.scene_path);
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            if (!string.IsNullOrEmpty(request.prefab_path))
                VerifyPrefab(request.prefab_path, objectsByName[request.prefab_object_name]);

            Scene reloadedScene = EditorSceneManager.OpenScene(
                request.scene_path, OpenSceneMode.Single);
            foreach (ConstructionObject item in request.objects)
                findings.Add(VerifySceneObject(reloadedScene, item, gameplayNamespace));

            bool allObjectsVerified = findings.All(item =>
                item.exists_after_reload && string.IsNullOrEmpty(item.error));
            return new ConstructionReport
            {
                success = allObjectsVerified,
                scene_path = request.scene_path,
                prefab_path = request.prefab_path,
                created_asset_paths = createdAssets.ToArray(),
                verification_findings = findings.ToArray(),
                errors = allObjectsVerified
                    ? Array.Empty<string>()
                    : findings.Where(item => !string.IsNullOrEmpty(item.error))
                        .Select(item => item.error).ToArray()
            };
        }

        private static void ValidateRequest(ConstructionRequest request)
        {
            if (request == null)
                throw new InvalidOperationException("Construction request is empty or invalid.");
            ValidateAssetPath(request.scene_path, ".unity");
            if (!string.IsNullOrEmpty(request.prefab_path))
                ValidateAssetPath(request.prefab_path, ".prefab");
            if (request.objects == null || request.objects.Length == 0)
                throw new InvalidOperationException("At least one scene object is required.");

            HashSet<string> names = new HashSet<string>(StringComparer.Ordinal);
            foreach (ConstructionObject item in request.objects)
            {
                if (item == null || string.IsNullOrWhiteSpace(item.name)
                    || !names.Add(item.name))
                    throw new InvalidOperationException(
                        "Scene object names must be present and unique.");
                if (!IsApprovedPrimitive(item.primitive))
                    throw new InvalidOperationException(
                        "Unapproved primitive: " + item.primitive);
                ValidateVector(item.position, 3, item.name + ".position");
                ValidateVector(item.scale, 3, item.name + ".scale");
                ValidateColor(item.color, item.name + ".color");
                if (item.scale.Any(value => value <= 0f))
                    throw new InvalidOperationException(
                        item.name + " scale values must be positive.");
                if (item.components != null)
                    foreach (ConstructionComponent component in item.components)
                        ValidateComponent(component);
            }

            if (!string.IsNullOrEmpty(request.prefab_path)
                && !names.Contains(request.prefab_object_name))
                throw new InvalidOperationException(
                    "Prefab source must match one requested scene object.");
            if (string.IsNullOrEmpty(request.prefab_path)
                && !string.IsNullOrEmpty(request.prefab_object_name))
                throw new InvalidOperationException(
                    "A prefab object name requires a prefab path.");

            foreach (string assetPath in GetExpectedAssetPaths(request))
            {
                string absolutePath = ResolveProjectAssetPath(assetPath);
                if (File.Exists(absolutePath) && !request.replace_existing)
                    throw new IOException(
                        "Refusing to overwrite existing asset: " + assetPath);
            }
        }

        private static IEnumerable<string> GetExpectedAssetPaths(
            ConstructionRequest request)
        {
            yield return request.scene_path;
            if (!string.IsNullOrEmpty(request.prefab_path))
                yield return request.prefab_path;
            string materialDirectory =
                Path.GetDirectoryName(request.scene_path).Replace('\\', '/') + "/Materials";
            foreach (string name in request.objects.Select(item => SafeAssetName(item.name))
                         .Distinct(StringComparer.Ordinal))
                yield return materialDirectory + "/" + name + "_Material.mat";
        }

        private static void ValidateAssetPath(string assetPath, string extension)
        {
            if (string.IsNullOrWhiteSpace(assetPath)
                || assetPath.Contains("\\")
                || Path.IsPathRooted(assetPath)
                || !assetPath.StartsWith(AllowedAssetRoot, StringComparison.Ordinal)
                || !assetPath.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Asset path must be a canonical " + extension
                    + " path beneath " + AllowedAssetRoot);

            string[] segments = assetPath.Split('/');
            if (segments.Any(segment => string.IsNullOrEmpty(segment)
                || segment == "." || segment == ".."))
                throw new InvalidOperationException(
                    "Asset path traversal is not allowed.");
        }

        private static string ResolveProjectAssetPath(string assetPath)
        {
            string projectRoot = Directory.GetParent(Application.dataPath).FullName;
            string absolutePath = Path.GetFullPath(
                Path.Combine(projectRoot, assetPath.Replace('/', Path.DirectorySeparatorChar)));
            string assetsRoot = Path.GetFullPath(Application.dataPath)
                + Path.DirectorySeparatorChar;
            if (!absolutePath.StartsWith(assetsRoot, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Resolved asset path escapes the configured Unity project Assets folder.");
            return absolutePath;
        }

        private static bool IsApprovedPrimitive(string primitive)
        {
            return primitive == "Cube" || primitive == "Sphere"
                || primitive == "Capsule" || primitive == "Cylinder"
                || primitive == "Plane" || primitive == "Quad";
        }

        private static void ValidateVector(float[] values, int length, string label)
        {
            if (values == null || values.Length != length
                || values.Any(value => float.IsNaN(value) || float.IsInfinity(value)))
                throw new InvalidOperationException(label + " must contain finite values.");
        }

        private static void ValidateColor(float[] values, string label)
        {
            if (values == null || (values.Length != 3 && values.Length != 4)
                || values.Any(value => float.IsNaN(value) || float.IsInfinity(value)
                    || value < 0f || value > 1f))
                throw new InvalidOperationException(
                    label + " channels must be finite values between 0 and 1.");
        }

        private static void ValidateComponent(ConstructionComponent component)
        {
            if (component == null)
                throw new InvalidOperationException("Component settings cannot be null.");
            switch (component.type)
            {
                case "BoxCollider":
                case "SphereCollider":
                case "CapsuleCollider":
                case "Rigidbody":
                case "Camera":
                case "Light":
                case "CharacterController":
                case "CourseGameManager":
                case "CoursePlayerController":
                case "CourseCameraFollow":
                case "CourseCollectible":
                case "CourseFinish":
                case "CourseHud":
                    return;
                default:
                    throw new InvalidOperationException(
                        "Component type is not approved: " + component.type);
            }
        }

        private static GameObject CreatePrimitive(ConstructionObject item)
        {
            PrimitiveType primitiveType;
            switch (item.primitive)
            {
                case "Cube": primitiveType = PrimitiveType.Cube; break;
                case "Sphere": primitiveType = PrimitiveType.Sphere; break;
                case "Capsule": primitiveType = PrimitiveType.Capsule; break;
                case "Cylinder": primitiveType = PrimitiveType.Cylinder; break;
                case "Plane": primitiveType = PrimitiveType.Plane; break;
                case "Quad": primitiveType = PrimitiveType.Quad; break;
                default:
                    throw new InvalidOperationException(
                        "Unapproved primitive: " + item.primitive);
            }

            GameObject gameObject = GameObject.CreatePrimitive(primitiveType);
            gameObject.name = item.name;
            return gameObject;
        }

        private static void ConfigureTransform(
            Transform transform, ConstructionObject item)
        {
            transform.position = new Vector3(
                item.position[0], item.position[1], item.position[2]);
            transform.localScale = new Vector3(
                item.scale[0], item.scale[1], item.scale[2]);
        }

        private static void ConfigureMaterial(
            GameObject gameObject,
            ConstructionObject item,
            string sceneDirectory,
            string sceneAssetDirectory,
            List<string> createdAssets)
        {
            Renderer renderer = gameObject.GetComponent<Renderer>();
            if (renderer == null)
                return;

            string materialDirectory = Path.Combine(sceneDirectory, "Materials");
            Directory.CreateDirectory(materialDirectory);
            AssetDatabase.Refresh();
            string materialPath = sceneAssetDirectory + "/Materials/"
                + SafeAssetName(item.name) + "_Material.mat";
            Shader shader = Shader.Find("Standard");
            if (shader == null)
                shader = Shader.Find("Universal Render Pipeline/Lit");
            if (shader == null)
                throw new InvalidOperationException(
                    "No supported Unity material shader was found.");
            Material material = new Material(shader);
            material.color = new Color(
                item.color[0], item.color[1], item.color[2],
                item.color.Length == 4 ? item.color[3] : 1f);
            AssetDatabase.CreateAsset(material, materialPath);
            renderer.sharedMaterial = material;
            createdAssets.Add(materialPath);
        }

        private static void ConfigureComponents(
            GameObject gameObject,
            ConstructionComponent[] components,
            Dictionary<string, GameObject> objectsByName,
            string gameplayNamespace)
        {
            if (components == null)
                return;
            foreach (ConstructionComponent settings in components)
            {
                switch (settings.type)
                {
                    case "BoxCollider":
                    {
                        BoxCollider component = GetOrAdd<BoxCollider>(gameObject);
                        if (settings.has_is_trigger)
                            component.isTrigger = settings.is_trigger;
                        break;
                    }
                    case "SphereCollider":
                    {
                        SphereCollider component = GetOrAdd<SphereCollider>(gameObject);
                        if (settings.has_is_trigger)
                            component.isTrigger = settings.is_trigger;
                        if (settings.has_radius)
                            component.radius = settings.radius;
                        break;
                    }
                    case "CapsuleCollider":
                    {
                        CapsuleCollider component = GetOrAdd<CapsuleCollider>(gameObject);
                        if (settings.has_is_trigger)
                            component.isTrigger = settings.is_trigger;
                        if (settings.has_radius)
                            component.radius = settings.radius;
                        if (settings.has_height)
                            component.height = settings.height;
                        break;
                    }
                    case "Rigidbody":
                    {
                        Rigidbody component = GetOrAdd<Rigidbody>(gameObject);
                        if (settings.has_mass)
                            component.mass = settings.mass;
                        if (settings.has_use_gravity)
                            component.useGravity = settings.use_gravity;
                        if (settings.has_is_kinematic)
                            component.isKinematic = settings.is_kinematic;
                        break;
                    }
                    case "Camera":
                    {
                        Camera component = GetOrAdd<Camera>(gameObject);
                        if (settings.has_field_of_view)
                            component.fieldOfView = settings.field_of_view;
                        break;
                    }
                    case "Light":
                    {
                        Light component = GetOrAdd<Light>(gameObject);
                        if (settings.has_intensity)
                            component.intensity = settings.intensity;
                        Renderer lightRenderer = gameObject.GetComponent<Renderer>();
                        if (lightRenderer != null)
                            lightRenderer.enabled = false;
                        break;
                    }
                    case "CharacterController":
                    {
                        Collider primitiveCollider = gameObject.GetComponent<Collider>();
                        if (primitiveCollider != null)
                            UnityEngine.Object.DestroyImmediate(primitiveCollider);
                        CharacterController component =
                            GetOrAdd<CharacterController>(gameObject);
                        if (settings.has_height)
                            component.height = settings.height;
                        if (settings.has_radius)
                            component.radius = settings.radius;
                        if (settings.has_slope_limit)
                            component.slopeLimit = settings.slope_limit;
                        if (settings.has_step_offset)
                            component.stepOffset = settings.step_offset;
                        break;
                    }
                    case "CourseGameManager":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "player",
                            ResolveObject(objectsByName, settings.player_name));
                        SetInt(
                            component, "requiredCollectibles",
                            settings.has_required_collectibles
                                ? settings.required_collectibles : 3);
                        HidePrimitiveRenderer(gameObject);
                        break;
                    }
                    case "CoursePlayerController":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "gameManager",
                            GetRuntimeComponent(
                                ResolveObject(objectsByName, settings.game_manager_name),
                                "CourseGameManager", gameplayNamespace));
                        SetObjectReference(
                            component, "cameraTransform",
                            ResolveObject(objectsByName, settings.camera_name).transform);
                        SetFloat(
                            component, "moveSpeed",
                            settings.has_move_speed ? settings.move_speed : 6f);
                        SetFloat(
                            component, "jumpHeight",
                            settings.has_jump_height ? settings.jump_height : 1.4f);
                        break;
                    }
                    case "CourseCameraFollow":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "target",
                            ResolveObject(objectsByName, settings.target_name).transform);
                        HidePrimitiveRenderer(gameObject);
                        break;
                    }
                    case "CourseCollectible":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "gameManager",
                            GetRuntimeComponent(
                                ResolveObject(objectsByName, settings.game_manager_name),
                                "CourseGameManager", gameplayNamespace));
                        break;
                    }
                    case "CourseFinish":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "gameManager",
                            GetRuntimeComponent(
                                ResolveObject(objectsByName, settings.game_manager_name),
                                "CourseGameManager", gameplayNamespace));
                        SetObjectReference(
                            component, "player",
                            ResolveObject(objectsByName, settings.player_name));
                        break;
                    }
                    case "CourseHud":
                    {
                        Component component = GetRuntimeComponent(
                            gameObject, settings.type, gameplayNamespace);
                        SetObjectReference(
                            component, "gameManager",
                            GetRuntimeComponent(
                                ResolveObject(objectsByName, settings.game_manager_name),
                                "CourseGameManager", gameplayNamespace));
                        HidePrimitiveRenderer(gameObject);
                        break;
                    }
                    default:
                        throw new InvalidOperationException(
                            "Component type is not approved: " + settings.type);
                }
            }
        }

        private static void EnsureComponents(
            GameObject gameObject,
            ConstructionComponent[] components,
            string gameplayNamespace)
        {
            if (components == null)
                return;
            foreach (ConstructionComponent settings in components)
            {
                switch (settings.type)
                {
                    case "BoxCollider": GetOrAdd<BoxCollider>(gameObject); break;
                    case "SphereCollider": GetOrAdd<SphereCollider>(gameObject); break;
                    case "CapsuleCollider": GetOrAdd<CapsuleCollider>(gameObject); break;
                    case "Rigidbody": GetOrAdd<Rigidbody>(gameObject); break;
                    case "Camera": GetOrAdd<Camera>(gameObject); break;
                    case "Light": GetOrAdd<Light>(gameObject); break;
                    case "CharacterController": GetOrAdd<CharacterController>(gameObject); break;
                    case "CourseGameManager":
                    case "CoursePlayerController":
                    case "CourseCameraFollow":
                    case "CourseCollectible":
                    case "CourseFinish":
                    case "CourseHud":
                        EnsureRuntimeComponent(gameObject, settings.type, gameplayNamespace);
                        break;
                    default:
                        throw new InvalidOperationException(
                            "Component type is not approved: " + settings.type);
                }
            }
        }

        private static string GetGameplayNamespace(string scenePath)
        {
            string[] segments = scenePath.Split('/');
            if (segments.Length < 5 || segments[0] != "Assets"
                || segments[1] != "UnityAgent" || segments[2] != "Generated"
                || string.IsNullOrEmpty(segments[3]))
                throw new InvalidOperationException(
                    "Gameplay components require a scene beneath the generated asset root.");

            string identifier = new string(segments[3].Select(character =>
                char.IsLetterOrDigit(character) || character == '_'
                    ? character : '_').ToArray());
            if (char.IsDigit(identifier[0]))
                identifier = "_" + identifier;
            return "UnityAgent.Generated." + identifier;
        }

        private static Type GetApprovedRuntimeType(
            string componentName, string gameplayNamespace)
        {
            switch (componentName)
            {
                case "CourseGameManager":
                case "CoursePlayerController":
                case "CourseCameraFollow":
                case "CourseCollectible":
                case "CourseFinish":
                case "CourseHud":
                    break;
                default:
                    throw new InvalidOperationException(
                        "Runtime component is not approved: " + componentName);
            }

            string fullName = gameplayNamespace + "." + componentName;
            Type[] matches = AppDomain.CurrentDomain.GetAssemblies()
                .Select(assembly => assembly.GetType(fullName, false, false))
                .Where(type => type != null).Distinct().ToArray();
            if (matches.Length != 1
                || !typeof(MonoBehaviour).IsAssignableFrom(matches[0]))
                throw new InvalidOperationException(
                    "Approved runtime component could not be resolved uniquely: " + fullName);
            return matches[0];
        }

        private static Component EnsureRuntimeComponent(
            GameObject gameObject, string componentName, string gameplayNamespace)
        {
            Type componentType = GetApprovedRuntimeType(componentName, gameplayNamespace);
            Component component = gameObject.GetComponent(componentType);
            return component != null ? component : gameObject.AddComponent(componentType);
        }

        private static Component GetRuntimeComponent(
            GameObject gameObject, string componentName, string gameplayNamespace)
        {
            return gameObject.GetComponent(
                GetApprovedRuntimeType(componentName, gameplayNamespace));
        }

        private static void SetObjectReference(
            Component component, string propertyName, UnityEngine.Object value)
        {
            SerializedObject serialized = new SerializedObject(component);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property == null
                || property.propertyType != SerializedPropertyType.ObjectReference
                || value == null)
                throw new InvalidOperationException(
                    "Approved object reference is missing or invalid: " + propertyName);
            property.objectReferenceValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static void SetInt(
            Component component, string propertyName, int value)
        {
            SerializedObject serialized = new SerializedObject(component);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property == null || property.propertyType != SerializedPropertyType.Integer)
                throw new InvalidOperationException(
                    "Approved integer property is missing or invalid: " + propertyName);
            property.intValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static void SetFloat(
            Component component, string propertyName, float value)
        {
            SerializedObject serialized = new SerializedObject(component);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property == null || property.propertyType != SerializedPropertyType.Float)
                throw new InvalidOperationException(
                    "Approved float property is missing or invalid: " + propertyName);
            property.floatValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static bool HasObjectReference(
            Component component, string propertyName)
        {
            SerializedObject serialized = new SerializedObject(component);
            SerializedProperty property = serialized.FindProperty(propertyName);
            return property != null
                && property.propertyType == SerializedPropertyType.ObjectReference
                && property.objectReferenceValue != null;
        }

        private static GameObject ResolveObject(
            Dictionary<string, GameObject> objectsByName, string objectName)
        {
            GameObject result;
            if (string.IsNullOrEmpty(objectName)
                || !objectsByName.TryGetValue(objectName, out result))
                throw new InvalidOperationException(
                    "Construction reference does not resolve to a requested object: "
                    + (objectName ?? "<null>"));
            return result;
        }

        private static void HidePrimitiveRenderer(GameObject gameObject)
        {
            Renderer renderer = gameObject.GetComponent<Renderer>();
            if (renderer != null)
                renderer.enabled = false;
        }

        private static T GetOrAdd<T>(GameObject gameObject) where T : Component
        {
            T component = gameObject.GetComponent<T>();
            return component != null ? component : gameObject.AddComponent<T>();
        }

        private static ConstructionFinding VerifySceneObject(
            Scene scene,
            ConstructionObject item,
            string gameplayNamespace)
        {
            GameObject found = scene.GetRootGameObjects()
                .FirstOrDefault(gameObject => gameObject.name == item.name);
            if (found == null)
                return new ConstructionFinding
                {
                    object_name = item.name,
                    exists_after_reload = false,
                    verified_components = Array.Empty<string>(),
                    error = "Scene object was not present after scene reload."
                };

            List<string> verified = new List<string>();
            if (found.GetComponent<Renderer>() != null)
                verified.Add("Renderer");
            if (found.GetComponent<Collider>() != null)
                verified.Add("Collider");
            foreach (ConstructionComponent settings in item.components
                         ?? Array.Empty<ConstructionComponent>())
            {
                if (HasComponent(found, settings.type, gameplayNamespace))
                {
                    verified.Add(settings.type);
                    string referenceError = VerifyComponentReferences(
                        found, settings.type, gameplayNamespace);
                    if (!string.IsNullOrEmpty(referenceError))
                        return new ConstructionFinding
                        {
                            object_name = item.name,
                            exists_after_reload = true,
                            verified_components = verified.ToArray(),
                            error = referenceError
                        };
                }
                else
                    return new ConstructionFinding
                    {
                        object_name = item.name,
                        exists_after_reload = true,
                        verified_components = verified.ToArray(),
                        error = "Expected component missing after reload: " + settings.type
                    };
            }

            return new ConstructionFinding
            {
                object_name = item.name,
                exists_after_reload = true,
                verified_components = verified.ToArray(),
                error = null
            };
        }

        private static string VerifyComponentReferences(
            GameObject gameObject,
            string componentType,
            string gameplayNamespace)
        {
            switch (componentType)
            {
                case "CourseGameManager":
                    return HasObjectReference(
                        GetRuntimeComponent(gameObject, componentType, gameplayNamespace),
                        "player")
                        ? null : "CourseGameManager player reference is missing after reload.";
                case "CoursePlayerController":
                {
                    Component component = GetRuntimeComponent(
                        gameObject, componentType, gameplayNamespace);
                    return HasObjectReference(component, "gameManager")
                        && HasObjectReference(component, "cameraTransform")
                        ? null : "CoursePlayerController references are missing after reload.";
                }
                case "CourseCameraFollow":
                    return HasObjectReference(
                        GetRuntimeComponent(gameObject, componentType, gameplayNamespace),
                        "target")
                        ? null : "CourseCameraFollow target is missing after reload.";
                case "CourseCollectible":
                    return HasObjectReference(
                        GetRuntimeComponent(gameObject, componentType, gameplayNamespace),
                        "gameManager")
                        ? null : "CourseCollectible manager reference is missing after reload.";
                case "CourseFinish":
                {
                    Component component = GetRuntimeComponent(
                        gameObject, componentType, gameplayNamespace);
                    return HasObjectReference(component, "gameManager")
                        && HasObjectReference(component, "player")
                        ? null : "CourseFinish references are missing after reload.";
                }
                case "CourseHud":
                    return HasObjectReference(
                        GetRuntimeComponent(gameObject, componentType, gameplayNamespace),
                        "gameManager")
                        ? null : "CourseHud manager reference is missing after reload.";
                default:
                    return null;
            }
        }

        private static bool HasComponent(
            GameObject gameObject, string componentType, string gameplayNamespace)
        {
            switch (componentType)
            {
                case "BoxCollider": return gameObject.GetComponent<BoxCollider>() != null;
                case "SphereCollider": return gameObject.GetComponent<SphereCollider>() != null;
                case "CapsuleCollider": return gameObject.GetComponent<CapsuleCollider>() != null;
                case "Rigidbody": return gameObject.GetComponent<Rigidbody>() != null;
                case "Camera": return gameObject.GetComponent<Camera>() != null;
                case "Light": return gameObject.GetComponent<Light>() != null;
                case "CharacterController": return gameObject.GetComponent<CharacterController>() != null;
                case "CourseGameManager":
                case "CoursePlayerController":
                case "CourseCameraFollow":
                case "CourseCollectible":
                case "CourseFinish":
                case "CourseHud":
                    return GetRuntimeComponent(
                        gameObject, componentType, gameplayNamespace) != null;
                default: return false;
            }
        }

        private static void VerifyPrefab(string prefabPath, GameObject prefabSource)
        {
            GameObject loaded = AssetDatabase.LoadAssetAtPath<GameObject>(prefabPath);
            if (loaded == null || loaded.name != prefabSource.name)
                throw new InvalidOperationException(
                    "Prefab could not be reloaded and verified: " + prefabPath);
            foreach (Component component in prefabSource.GetComponents<Component>())
                if (component != null
                    && loaded.GetComponent(component.GetType()) == null)
                    throw new InvalidOperationException(
                        "Prefab is missing a source component: "
                        + component.GetType().Name);
        }

        private static string SafeAssetName(string value)
        {
            StringBuilder safe = new StringBuilder();
            bool replacing = false;
            foreach (char character in value)
            {
                bool approved = character >= 'A' && character <= 'Z'
                    || character >= 'a' && character <= 'z'
                    || character >= '0' && character <= '9'
                    || character == '_' || character == '-';
                if (approved)
                {
                    safe.Append(character);
                    replacing = false;
                }
                else if (!replacing)
                {
                    safe.Append('_');
                    replacing = true;
                }
            }

            string result = safe.ToString().Trim('_');
            return string.IsNullOrEmpty(result) ? "Object" : result;
        }
    }
}
