package com.kvmlite.app

import android.os.Bundle
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.kvmlite.app.ui.KvmScreen

class MainActivity : ComponentActivity() {

    private lateinit var controller: KvmController

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }

        controller = KvmController()
        intent?.getStringExtra("url")?.let { controller.serverUrl = it }
        intent?.getStringExtra("hidpath")?.let { controller.setHidPath(it) }
        setContent {
            KvmScreen(controller)
        }
        if (intent?.getBooleanExtra("connect", false) == true) controller.connect()
    }

    /**
     * `am start ... --es url ws://... --ez connect true` re-targets a running
     * instance, which is how the benchmark scripts point the phone at a
     * specific agent port without reinstalling.
     */
    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra("hidpath")?.let { controller.setHidPath(it) }
        intent.getStringExtra("url")?.let { controller.serverUrl = it }
        if (intent.getBooleanExtra("connect", false)) controller.connect()
    }

    /**
     * Hardware keyboard (USB/BT) is mirrored to the PC. We intercept before the
     * Compose view so letters never land in local widgets while HID is active.
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (::controller.isInitialized && controller.handleKeyEvent(event)) return true
        return super.dispatchKeyEvent(event)
    }

    override fun onPause() {
        super.onPause()
        if (::controller.isInitialized) controller.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (::controller.isInitialized) controller.shutdown()
    }
}
