package org.zotero.android.screens.reader

import timber.log.Timber

/**
 * The dashboard swaps between separate phone and tablet navigation trees when the window size
 * class changes — e.g. when a foldable is opened or closed — which destroys the open reader.
 * This remembers the open document across that swap so the new tree can reopen it, together with
 * the AI panel if it was showing.
 */
object ReaderSessionRestore {
    private var openReader: Pair<String, String>? = null
    private var pendingReader: Pair<String, String>? = null
    private var pendingAiPanel = false

    /** Whether the AI panel is showing in the open reader. */
    var isAiPanelOpen = false

    internal fun onReaderOpened(readerParams: String, encodedFilePath: String) {
        openReader = readerParams to encodedFilePath
    }

    internal fun onReaderClosed() {
        Timber.i("ReaderSessionRestore: reader closed")
        openReader = null
        isAiPanelOpen = false
    }

    /**
     * Keys the dashboard's navigation tree. It changes when a fold/unfold happens with a document
     * open, so the new tree starts fresh instead of restoring a stale copy of the reader, and
     * [consumePendingReader] reopens the document cleanly.
     */
    var navigationGeneration = 0
        private set

    /** Called while composing the new layout, before the old navigation tree is disposed. */
    fun onLayoutTypeChanged() {
        pendingReader = openReader
        pendingAiPanel = openReader != null && isAiPanelOpen
        if (openReader != null) navigationGeneration++
        Timber.i("ReaderSessionRestore: layout changed, reopen=${pendingReader != null}, aiPanel=$pendingAiPanel")
    }

    fun consumePendingReader(): Pair<String, String>? = pendingReader.also { pendingReader = null }

    fun consumePendingAiPanel(): Boolean = pendingAiPanel.also { pendingAiPanel = false }
}
