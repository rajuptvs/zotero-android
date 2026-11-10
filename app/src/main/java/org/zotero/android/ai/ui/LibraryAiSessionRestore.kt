package org.zotero.android.ai.ui

/**
 * Folding or unfolding a foldable swaps the dashboard's phone and tablet layouts, which recreates
 * the library screen. This keeps whether the library AI panel was open, and the conversation it
 * showed, so the new screen reopens both (the reader has ReaderSessionRestore for the same reason).
 * Lives as long as the process; a fresh start opens a fresh chat.
 */
object LibraryAiSessionRestore {
    var isPanelOpen = false
    var noteKey: String? = null
}
