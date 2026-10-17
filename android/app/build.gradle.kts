// The Wordbot app: Kotlin for the phone's side (screen capture, accessibility gestures, notification, widget,
// the dashboard's window); the bot itself is the project's Python code (wordbot/), run inside the app by Chaquopy.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.wordbot.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.wordbot.app"
        minSdk = 30
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        ndk {
            abiFilters += listOf("arm64-v8a")  // 64-bit ARM phones
            if (providers.gradleProperty("wordbot.emulator").isPresent) abiFilters += "x86_64"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    androidResources { noCompress += "traineddata" }
}

// The bot's Python files, copied from the project: the wordbot package (code, dashboard page, image templates),
// what the PC bot has learned so far (letter shapes, pictures) and the word list, plus the app's entry points
// and the pytesseract stand-in.
val botRoot = rootProject.projectDir.parentFile
val stagePython by tasks.registering(Sync::class) {
    from(File(botRoot, "wordbot")) {
        include("*.py", "static/**", "assets/**")
        into("wordbot")
    }
    from(File(botRoot, "templates")) { include("*.png"); into("wordbot/seed/templates") }
    from(File(botRoot, "data/pictures")) { include("*.png"); into("wordbot/seed/pictures") }
    from(File(botRoot, "data")) { include("words_en.txt.gz"); into("wordbot/seed") }
    from(File(rootProject.projectDir, "python"))
    exclude("**/__pycache__/**")
    into(layout.buildDirectory.dir("python-src"))
}
tasks.named("preBuild") { dependsOn(stagePython) }
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach { dependsOn(stagePython) }

chaquopy {
    defaultConfig {
        version = "3.10"
        buildPython(providers.gradleProperty("wordbot.buildPython").getOrElse("python3.10"))
        pip {
            install("numpy==1.26.2")
            install("opencv-python==4.5.1.48")
        }
        // On disk rather than read from the APK: the bot opens its page, templates and word list by path.
        extractPackages("wordbot")
        pyc { src = false }
    }
    sourceSets {
        getByName("main") { srcDir(layout.buildDirectory.dir("python-src")) }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("cz.adaptech.tesseract4android:tesseract4android:4.9.0")
}
