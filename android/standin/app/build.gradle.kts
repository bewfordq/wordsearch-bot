// A stand-in for Word Search on the emulator (tools/standin_levels.py builds its levels). Same package name as
// the game, so it can't be installed on the phone next to the real one.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}
android {
    namespace = "in.playsimple.wordsearch"
    compileSdk = 34
    defaultConfig { applicationId = "in.playsimple.wordsearch"; minSdk = 30; targetSdk = 34; versionCode = 1; versionName = "test" }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    androidResources { noCompress += "png" }
}
