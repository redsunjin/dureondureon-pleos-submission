import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.net.URI

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val pleosEnabled = providers.gradleProperty("pleosEnabled").orElse("false").get().toBoolean()
val submissionDemo = providers.gradleProperty("submissionDemo").orElse("false").get().let {
    require(it == "true" || it == "false") { "submissionDemo must be true or false" }
    it.toBoolean()
}
val releaseCoordinationBaseUrl = providers.gradleProperty("releaseCoordinationBaseUrl")
    .orElse(if (submissionDemo) "http://127.0.0.1:8765" else "https://localhost")
    .get()
if (submissionDemo) {
    val endpoint = URI(releaseCoordinationBaseUrl)
    require(endpoint.scheme == "http" && endpoint.host == "127.0.0.1" && endpoint.port in 1..65535 &&
        endpoint.rawPath.isNullOrEmpty() && endpoint.rawQuery == null && endpoint.rawFragment == null && endpoint.rawUserInfo == null) {
        "Submission simulation requires an explicit loopback HTTP endpoint and port"
    }
}
fun buildConfigString(value: String): String =
    "\"${value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r")}\""

// Release signing is deliberately opt-in and local-only. Never put these values in source,
// gradle.properties, local.properties, or a committed CI configuration.
val releaseSigningKeystore = System.getenv("SWARM_RELEASE_KEYSTORE")?.trim().orEmpty()
val releaseSigningAlias = System.getenv("SWARM_RELEASE_KEY_ALIAS")?.trim().orEmpty()
val releaseSigningStorePassword = System.getenv("SWARM_RELEASE_STORE_PASSWORD").orEmpty()
val releaseSigningKeyPassword = System.getenv("SWARM_RELEASE_KEY_PASSWORD").orEmpty()
val releaseSigningValues = listOf(
    releaseSigningKeystore,
    releaseSigningAlias,
    releaseSigningStorePassword,
    releaseSigningKeyPassword,
)
val releaseSigningConfigured = releaseSigningValues.any { it.isNotEmpty() }
val releaseSigningComplete = releaseSigningValues.all { it.isNotEmpty() }
if (releaseSigningConfigured && !releaseSigningComplete) {
    error("SWARM_RELEASE_KEYSTORE, SWARM_RELEASE_KEY_ALIAS, SWARM_RELEASE_STORE_PASSWORD, and SWARM_RELEASE_KEY_PASSWORD must be provided together")
}
if (releaseSigningComplete && !file(releaseSigningKeystore).isFile) {
    error("SWARM_RELEASE_KEYSTORE does not point to a readable keystore file")
}

android {
    namespace = "io.swarm.mobility"
    compileSdk = 35
    buildFeatures { buildConfig = true }
    sourceSets.getByName("main").java.srcDir(if (pleosEnabled) "src/pleos/java" else "src/local/java")
    if (submissionDemo) {
        sourceSets.getByName("release").manifest.srcFile("src/submissionDemo/AndroidManifest.xml")
        sourceSets.getByName("release").res.srcDir("src/submissionDemo/res")
    }
    signingConfigs {
        if (releaseSigningComplete) {
            create("submissionRelease") {
                storeFile = file(releaseSigningKeystore)
                storePassword = releaseSigningStorePassword
                keyAlias = releaseSigningAlias
                keyPassword = releaseSigningKeyPassword
            }
        }
    }
    buildTypes {
        getByName("debug") {
            buildConfigField("boolean", "SUBMISSION_DEMO_ENABLED", "false")
            buildConfigField("String", "COORDINATION_BASE_URL", "\"http://127.0.0.1:8765\"")
            buildConfigField("boolean", "APP_MAP_ENABLED", "true")
            buildConfigField("boolean", "CURB_DEMO_ENABLED", "true")
            buildConfigField("String", "MAP_TILE_BASE_URL", "\"https://tile.openstreetmap.org\"")
            buildConfigField("String", "MAP_ROUTE_BASE_URL", "\"https://router.project-osrm.org\"")
        }
        getByName("release") {
            isDebuggable = false
            buildConfigField("boolean", "SUBMISSION_DEMO_ENABLED", submissionDemo.toString())
            if (submissionDemo) applicationIdSuffix = ".submission"
            buildConfigField("String", "COORDINATION_BASE_URL", buildConfigString(releaseCoordinationBaseUrl))
            buildConfigField("boolean", "APP_MAP_ENABLED", "false")
            buildConfigField("boolean", "CURB_DEMO_ENABLED", submissionDemo.toString())
            buildConfigField("String", "MAP_TILE_BASE_URL", "\"\"")
            buildConfigField("String", "MAP_ROUTE_BASE_URL", "\"\"")
            if (releaseSigningComplete) {
                signingConfig = signingConfigs.getByName("submissionRelease")
            }
        }
    }

    defaultConfig {
        applicationId = "io.swarm.mobility"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        buildConfigField("boolean", "PLEOS_ENABLED", pleosEnabled.toString())
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    if (pleosEnabled) {
        implementation("ai.pleos.playground:Vehicle:2.3.2")
        implementation("ai.pleos.playground:NaviHelper:3.0.5")
        implementation("ai.pleos.playground:FusedLocation:1.1.3")
    }
}
