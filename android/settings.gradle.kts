import org.gradle.api.initialization.resolve.RepositoriesMode

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        if (providers.gradleProperty("pleosEnabled").orElse("false").get().toBoolean()) {
            maven {
                url = uri("https://nexus-playground.pleos.ai/repository/maven-releases/")
                content { includeGroup("ai.pleos.playground") }
            }
        }
    }
}

rootProject.name = "swarm-mobility-android"
include(":app")
