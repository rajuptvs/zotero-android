package org.zotero.android.screens.reader

import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import org.zotero.android.uicomponents.Strings
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Reader and AI panel next to each other, so the document stays visible while chatting:
 * side by side when the window is wide (unfolded foldables, tablets, landscape), stacked with the
 * document on top on a phone. The divider between them can be dragged.
 *
 * The reader is always the first child of the same layout, so opening, closing or re-arranging
 * the panel never recreates the reader's WebView.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun ReaderAiSplitLayout(
    showPanel: Boolean,
    wide: Boolean,
    reader: @Composable () -> Unit,
    panel: @Composable () -> Unit,
) {
    // Side by side only when both fit (320 dp panel + 280 dp content + handle); a narrow pane, like
    // the item list next to the collections on an unfolded foldable, stacks instead.
    BoxWithConstraints(modifier = Modifier.fillMaxSize()) {
        SplitLayout(showPanel, wide && maxWidth >= 616.dp, reader, panel)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun SplitLayout(
    showPanel: Boolean,
    wide: Boolean,
    reader: @Composable () -> Unit,
    panel: @Composable () -> Unit,
) {
    var sideFraction by rememberSaveable { mutableFloatStateOf(0.42f) }
    var stackedFraction by rememberSaveable { mutableFloatStateOf(0.5f) }
    var totalSize by remember { mutableStateOf(0f) }
    val imeVisible = WindowInsets.isImeVisible
    val handleThickness = with(LocalDensity.current) { 16.dp.roundToPx() }
    val minPanelWidth = with(LocalDensity.current) { 320.dp.roundToPx() }
    val minReaderWidth = with(LocalDensity.current) { 280.dp.roundToPx() }

    Layout(
        modifier = Modifier.fillMaxSize().imePadding(),
        contents = listOf(
            {
                // Stacked, the top pane no longer reaches the bottom of the screen, so it mustn't
                // pad itself for the navigation bar (the panel below does).
                val stacked = showPanel && !wide
                Box(
                    modifier = if (stacked) {
                        Modifier.consumeWindowInsets(WindowInsets.navigationBars)
                    } else {
                        Modifier
                    }
                ) { reader() }
            },
            {
                if (showPanel) {
                    SplitHandle(
                        vertical = wide,
                        onDrag = { delta ->
                            if (totalSize > 0f) {
                                if (wide) {
                                    sideFraction = (sideFraction - delta / totalSize).coerceIn(0.28f, 0.72f)
                                } else {
                                    stackedFraction = (stackedFraction - delta / totalSize).coerceIn(0.25f, 0.85f)
                                }
                            }
                        }
                    )
                }
            },
            { if (showPanel) Box { panel() } },
        )
    ) { (readerMeasurables, handleMeasurables, panelMeasurables), constraints ->
        val width = constraints.maxWidth
        val height = constraints.maxHeight
        totalSize = (if (wide) width else height).toFloat()
        if (!showPanel || panelMeasurables.isEmpty()) {
            val readerPlaceable = readerMeasurables.first().measure(Constraints.fixed(width, height))
            return@Layout layout(width, height) { readerPlaceable.place(0, 0) }
        }
        if (wide) {
            val panelWidth = ((width - handleThickness) * sideFraction).roundToInt()
                .coerceIn(minPanelWidth, width - handleThickness - minReaderWidth)
            val readerWidth = width - handleThickness - panelWidth
            val readerPlaceable = readerMeasurables.first().measure(Constraints.fixed(readerWidth, height))
            val handle = handleMeasurables.firstOrNull()?.measure(Constraints.fixed(handleThickness, height))
            val panelPlaceable = panelMeasurables.first().measure(Constraints.fixed(panelWidth, height))
            layout(width, height) {
                readerPlaceable.place(0, 0)
                handle?.place(readerWidth, 0)
                panelPlaceable.place(readerWidth + handleThickness, 0)
            }
        } else {
            // While typing, give the conversation most of the (smaller) screen.
            val fraction = if (imeVisible) maxOf(stackedFraction, 0.8f) else stackedFraction
            val panelHeight = ((height - handleThickness) * fraction).roundToInt()
            val readerHeight = height - handleThickness - panelHeight
            val readerPlaceable = readerMeasurables.first().measure(Constraints.fixed(width, readerHeight))
            val handle = handleMeasurables.firstOrNull()?.measure(Constraints.fixed(width, handleThickness))
            val panelPlaceable = panelMeasurables.first().measure(Constraints.fixed(width, panelHeight))
            layout(width, height) {
                readerPlaceable.place(0, 0)
                handle?.place(0, readerHeight)
                panelPlaceable.place(0, readerHeight + handleThickness)
            }
        }
    }
}

@Composable
private fun SplitHandle(vertical: Boolean, onDrag: (Float) -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surfaceContainer)
            .pointerInput(vertical) {
                detectDragGestures { change, dragAmount ->
                    change.consume()
                    onDrag(if (vertical) dragAmount.x else dragAmount.y)
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        Box(
            modifier = Modifier
                .size(width = if (vertical) 4.dp else 40.dp, height = if (vertical) 40.dp else 4.dp)
                .background(MaterialTheme.colorScheme.outline, RoundedCornerShape(2.dp))
        )
    }
}

/**
 * Drawn over the reader while the user drags out the part of the page to send as an image.
 * Reports the rectangle in window pixels.
 */
@Composable
internal fun ReaderAreaCaptureOverlay(
    onCaptured: (android.graphics.Rect) -> Unit,
    onCancel: () -> Unit,
) {
    var origin by remember { mutableStateOf(Offset.Zero) }
    var start by remember { mutableStateOf<Offset?>(null) }
    var end by remember { mutableStateOf<Offset?>(null) }
    val density = LocalDensity.current
    Box(
        modifier = Modifier
            .fillMaxSize()
            .onGloballyPositioned { origin = it.boundsInWindow().topLeft }
            .background(Color.Black.copy(alpha = 0.15f))
            .pointerInput(Unit) {
                detectDragGestures(
                    onDragStart = { start = it; end = it },
                    onDrag = { change, amount ->
                        change.consume()
                        end = (end ?: change.position) + amount
                    },
                    onDragEnd = {
                        val s = start
                        val e = end
                        start = null
                        end = null
                        if (s != null && e != null && abs(e.x - s.x) > 24f && abs(e.y - s.y) > 24f) {
                            onCaptured(
                                android.graphics.Rect(
                                    (origin.x + min(s.x, e.x)).roundToInt(),
                                    (origin.y + min(s.y, e.y)).roundToInt(),
                                    (origin.x + maxOf(s.x, e.x)).roundToInt(),
                                    (origin.y + maxOf(s.y, e.y)).roundToInt(),
                                )
                            )
                        }
                    },
                    onDragCancel = { start = null; end = null },
                )
            }
    ) {
        val s = start
        val e = end
        if (s != null && e != null) {
            val left = min(s.x, e.x)
            val top = min(s.y, e.y)
            Box(
                modifier = Modifier
                    .offset { IntOffset(left.roundToInt(), top.roundToInt()) }
                    .size(
                        width = with(density) { abs(e.x - s.x).toDp() },
                        height = with(density) { abs(e.y - s.y).toDp() },
                    )
                    .border(2.dp, MaterialTheme.colorScheme.primary)
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f))
            )
        }
        Surface(
            modifier = Modifier.align(Alignment.TopCenter).padding(top = 12.dp),
            shape = RoundedCornerShape(20.dp),
            color = MaterialTheme.colorScheme.inverseSurface,
        ) {
            androidx.compose.foundation.layout.Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(start = 16.dp),
            ) {
                Text(
                    text = stringResource(Strings.reader_chat_capture_hint),
                    color = MaterialTheme.colorScheme.inverseOnSurface,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = onCancel) {
                    Text(
                        stringResource(Strings.reader_chat_cancel),
                        color = MaterialTheme.colorScheme.inversePrimary
                    )
                }
            }
        }
    }
}
