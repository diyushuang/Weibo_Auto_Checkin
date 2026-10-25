plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 稳定签名 keystore（PKCS12，别名 wcmsign）：本地放 app/keystore/（已 gitignore），
// CI 由 Secret WCM_KEYSTORE_B64 解出到同一路径。Android 只允许同签名 APK 覆盖安装，
// 缺失该文件时回退默认 debug 密钥，覆盖旧版会报「签名冲突」。
val wcmKeystore = file("keystore/wcm-release.p12").takeIf { it.exists() }
val wcmStorePassword = System.getenv("WCM_KEYSTORE_PASSWORD") ?: "wcm-checkin"

android {
    namespace = "com.wcm.checkin"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.wcm.checkin"
        minSdk = 26
        targetSdk = 35
        versionCode = 8
        versionName = "0.4.2"
    }

    signingConfigs {
        if (wcmKeystore != null) {
            create("wcm") {
                storeFile = wcmKeystore
                storeType = "PKCS12"
                storePassword = wcmStorePassword
                keyAlias = "wcmsign"
                keyPassword = wcmStorePassword
            }
        }
    }

    buildTypes {
        debug {
            signingConfigs.findByName("wcm")?.let { signingConfig = it }
        }
        release {
            isMinifyEnabled = false
            signingConfigs.findByName("wcm")?.let { signingConfig = it }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.1")
    implementation("androidx.fragment:fragment-ktx:1.8.2")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.viewpager2:viewpager2:1.1.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    testImplementation("junit:junit:4.13.2")
    // 本地单测跑在 JVM，android.jar 里的 org.json 是抛 Stub! 的桩，换真实实现
    testImplementation("org.json:json:20240303")
}
