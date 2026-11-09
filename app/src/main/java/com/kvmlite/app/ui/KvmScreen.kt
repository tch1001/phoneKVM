package com.kvmlite.app.ui

import android.view.SurfaceHolder
import android.view.SurfaceView
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.kvmlite.app.KvmController
import com.kvmlite.app.net.LinkState
import com.kvmlite.app.net.StreamStats
import java.util.Locale

@Composable
fun KvmScreen(c: KvmController) {
    val stats by c.stats.collectAsState()
    val frame by c.frame.collectAsState()
    var panelOpen by remember { mutableStateOf(false) }

    MaterialTheme(colorScheme = darkColorScheme()) {
        Box(Modifier.fillMaxSize().background(Color.Black)) {
            if (stats.surfaceMode) {
                // Hardware path: MediaCodec renders straight onto the
                // SurfaceView, no Bitmap and no recomposition per frame.
                HardwareVideo(c, stats)
            } else {
                frame?.let { vf ->
                    Image(
                        bitmap = vf.bitmap.asImageBitmap(),
                        contentDescription = "remote desktop",
                        modifier = Modifier.fillMaxSize(),
                        contentScale = ContentScale.Fit,
                    )
                    // Report when Compose actually put this frame on screen:
                    // withFrameNanos fires on the choreographer tick that
                    // composes and draws it, which closes the latency chain.
                    LaunchedEffect(vf.seq) {
                        withFrameNanos { ns -> c.onFrameDrawn(vf.seq, ns / 1_000_000) }
                    }
                }
            }

            if (frame == null && !stats.surfaceMode) {
                Text(
                    text = "no video\nconnect to the PC agent and press Connect",
                    color = Color(0xFF9AA0A6),
                    modifier = Modifier.align(Alignment.Center),
                )
            }

            // Transparent touch layer above the video so the SurfaceView can
            // never swallow gestures meant for the HID touchpad.
            Box(Modifier.fillMaxSize().pointerInput(Unit) { touchpad(c) })

            StatusPill(
                stats = stats,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(10.dp),
                onClick = { panelOpen = !panelOpen },
            )

            AnimatedVisibility(
                visible = panelOpen,
                modifier = Modifier.align(Alignment.BottomCenter),
            ) {
                ControlPanel(c, stats) { panelOpen = false }
            }
        }
    }
}

/**
 * SurfaceView + persistent MediaCodec decoder.
 *
 * The view is sized to the stream's aspect ratio inside the available space so
 * a 1920x1080 stream maps 1:1 onto the phone's 2400x1080 panel when rotated
 * instead of being resampled by the compositor.
 */
@Composable
private fun HardwareVideo(c: KvmController, stats: StreamStats) {
    val aspect = if (stats.width > 0 && stats.height > 0)
        stats.width.toFloat() / stats.height else 16f / 9f
    BoxWithConstraints(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        var w = maxWidth
        var h = w / aspect
        if (h > maxHeight) {
            h = maxHeight
            w = h * aspect
        }
        AndroidView(
            factory = { ctx ->
                SurfaceView(ctx).apply {
                    holder.addCallback(object : SurfaceHolder.Callback {
                        override fun surfaceCreated(holder: SurfaceHolder) {
                            c.onSurface(holder.surface)
                        }
                        override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
                            c.onSurface(holder.surface)
                        }
                        override fun surfaceDestroyed(holder: SurfaceHolder) {
                            c.onSurface(null)
                        }
                    })
                    isClickable = false
                    isFocusable = false
                }
            },
            modifier = Modifier.size(w, h),
        )
    }
}

@Composable
private fun StatusPill(stats: StreamStats, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val dot = when (stats.state) {
        LinkState.CONNECTED -> Color(0xFF4CD964)
        LinkState.CONNECTING, LinkState.RECONNECTING -> Color(0xFFFFD60A)
        LinkState.ERROR -> Color(0xFFFF453A)
        else -> Color(0xFF8E8E93)
    }
    val latency = if (stats.latencyAvgMs > 0) "${stats.latencyAvgMs} ms" else "-- ms"
    val line = when (stats.state) {
        LinkState.CONNECTED ->
            String.format(
                Locale.US,
                "%.0f fps  %.1f Mb/s  %s  %s",
                stats.fps, stats.kbps / 1000f, stats.codec.ifEmpty { "jpeg" },
                latency,
            )
        else -> stats.note.ifEmpty { stats.state.name.lowercase() }
    }
    Surface(
        modifier = modifier.clickable(onClick = onClick),
        color = Color.Black.copy(alpha = 0.62f),
        shape = RoundedCornerShape(50),
    ) {
        Row(
            Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.size(8.dp).background(dot, RoundedCornerShape(50)))
            Text(
                text = "  ${stats.state.name}  $line",
                color = Color.White,
                fontSize = 12.sp,
                fontFamily = FontFamily.Monospace,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun ControlPanel(c: KvmController, stats: StreamStats, onClose: () -> Unit) {
    Surface(color = Color(0xF01C1C1E), shape = RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp)) {
        Column(
            Modifier
                .fillMaxWidth()
                .heightIn(max = 460.dp)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("KVM-lite", fontWeight = FontWeight.Bold, color = Color.White)
                Text("hide", color = Color(0xFF8AB4F8), fontSize = 13.sp,
                    modifier = Modifier.padding(end = 4.dp).clickable(onClick = onClose))
            }

            OutlinedTextField(
                value = c.serverUrl,
                onValueChange = { c.serverUrl = it },
                label = { Text("WebSocket url") },
                singleLine = true,
                modifier = Modifier
                    .fillMaxWidth()
                    .onFocusChanged { c.textFieldFocused = it.isFocused },
            )

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { c.connect() }) { Text("Connect") }
                OutlinedButton(onClick = { c.disconnect() }) { Text("Disconnect") }
                OutlinedButton(onClick = onClose) { Text("Close") }
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                Switch(checked = c.hidEnabled, onCheckedChange = { c.setHid(it) })
                Text(
                    "  USB HID  ",
                    color = Color.White,
                    modifier = Modifier.padding(start = 10.dp),
                )
                Text(
                    c.hidStatus,
                    color = if (c.hidEnabled) Color(0xFF4CD964) else Color(0xFFFF9F0A),
                    fontSize = 12.sp,
                    fontFamily = FontFamily.Monospace,
                )
            }

            Label("Pointer sensitivity  ${"%.2f".format(c.sensitivity)}")
            Slider(
                value = c.sensitivity,
                onValueChange = { c.sensitivity = it },
                valueRange = 0.3f..4f,
            )

            Label("Scroll step  ${c.scrollStepPx.toInt()} px/tick")
            Slider(
                value = c.scrollStepPx,
                onValueChange = { c.scrollStepPx = it },
                valueRange = 16f..128f,
            )

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { c.setMouseReportLength(4) }) {
                    Text(if (c.reportLength == 4) "● 4-byte boot" else "4-byte boot")
                }
                OutlinedButton(onClick = { c.setMouseReportLength(8) }) {
                    Text(if (c.reportLength == 8) "● 8-byte ext" else "8-byte ext")
                }
                OutlinedButton(onClick = { c.setMouseReportLength(3) }) {
                    Text(if (c.reportLength == 3) "● 3-byte" else "3-byte")
                }
            }

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { c.selfTest() }, modifier = Modifier.weight(1f)) {
                    Text("Self-test: square + hello")
                }
            }

            OutlinedTextField(
                value = c.typedText,
                onValueChange = { c.typedText = it },
                label = { Text("Type on remote (US layout)") },
                singleLine = true,
                modifier = Modifier
                    .fillMaxWidth()
                    .onFocusChanged { c.textFieldFocused = it.isFocused },
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { c.typeText(c.typedText) }) { Text("Type") }
                OutlinedButton(onClick = { c.releaseAll() }) { Text("Release all") }
            }

            Text(
                String.format(
                    Locale.US,
                    "frames %d   %.1f fps   %d kbps   %dx%d   %s\n" +
                        "latency %d ms (median %.0f)   decode %.1f ms   %s",
                    stats.frames, stats.fps, stats.kbps, stats.width, stats.height, stats.note,
                    stats.latencyMs, stats.latencyAvgMs.toFloat(), stats.decodeMs,
                    if (stats.surfaceMode) "MediaCodec -> SurfaceView" else "JPEG -> Bitmap",
                ),
                color = Color(0xFF9AA0A6),
                fontSize = 11.sp,
                fontFamily = FontFamily.Monospace,
            )
        }
    }
}

@Composable
private fun Label(text: String) {
    Text(text, color = Color(0xFFC7C7CC), fontSize = 13.sp)
}
