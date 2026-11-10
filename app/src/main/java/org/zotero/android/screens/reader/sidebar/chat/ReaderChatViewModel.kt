package org.zotero.android.screens.reader.sidebar.chat

import android.net.Uri
import androidx.lifecycle.viewModelScope
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.zotero.android.ai.api.ChatGptModel
import org.zotero.android.ai.api.ChatGptResponsesClient
import org.zotero.android.ai.api.ChatImage
import org.zotero.android.ai.api.ChatStreamEvent
import org.zotero.android.ai.api.ChatStreamPhase
import org.zotero.android.ai.api.ChatTurn
import org.zotero.android.ai.auth.ChatGptAuthManager
import org.zotero.android.ai.auth.ChatGptAuthState
import org.zotero.android.ai.auth.ChatGptCredentialStore
import org.zotero.android.ai.chat.ChatMarkdown
import org.zotero.android.ai.chat.ChatNoteFormat
import org.zotero.android.architecture.BaseViewModel2
import org.zotero.android.architecture.ViewEffect
import org.zotero.android.architecture.ViewState
import org.zotero.android.architecture.coroutines.Dispatchers
import org.zotero.android.database.DbWrapperMain
import org.zotero.android.database.objects.ItemTypes
import org.zotero.android.database.requests.ChatNoteSnapshot
import org.zotero.android.database.requests.CreateNoteDbRequest
import org.zotero.android.database.requests.EditNoteDbRequest
import org.zotero.android.database.requests.ReadChatItemContextDbRequest
import org.zotero.android.database.requests.ReadChatNotesDbRequest
import org.zotero.android.database.requests.ReadItemTitleDbRequest
import org.zotero.android.screens.reader.ReaderChatContext
import org.zotero.android.sync.KeyGenerator
import org.zotero.android.sync.Note
import org.zotero.android.sync.SchemaController
import timber.log.Timber
import javax.inject.Inject

/** A saved conversation about this document, for the history menu. */
internal data class ChatHistoryEntry(val noteKey: String, val title: String)

/** A one-tap prompt for the attached selection. */
internal enum class ChatQuickAction(val label: String, val prompt: String) {
    Explain("Explain", "Explain this passage in the context of the paper."),
    Simplify("Simplify", "Explain this passage in simple terms, as if to a smart newcomer to the field."),
    Translate("Translate", "Translate this passage into English (or, if it is English, into plain everyday English) and briefly note any terms that don't translate cleanly."),
    Define("Define terms", "Define the technical terms and symbols in this passage, as they're used in this paper."),
}

@HiltViewModel
internal class ReaderChatViewModel @Inject constructor(
    private val authManager: ChatGptAuthManager,
    private val credentialStore: ChatGptCredentialStore,
    private val client: ChatGptResponsesClient,
    private val dbWrapperMain: DbWrapperMain,
    private val schemaController: SchemaController,
    private val dispatchers: Dispatchers,
) : BaseViewModel2<ReaderChatViewState, ReaderChatViewEffect>(ReaderChatViewState()) {

    private lateinit var contextProvider: suspend (includeDocumentText: Boolean) -> ReaderChatContext
    private var sendJob: Job? = null
    private var signInJob: Job? = null
    private var modelsJob: Job? = null
    private var documentTitle: String = ""
    private var itemContext: String = ""

    fun start(contextProvider: suspend (includeDocumentText: Boolean) -> ReaderChatContext) =
        initOnce {
            this.contextProvider = contextProvider
            updateState { copy(autoAttachSelection = credentialStore.autoAttachSelection) }
            authManager.state
                .onEach { state ->
                    updateState { copy(auth = state) }
                    if (state is ChatGptAuthState.SignedIn && state.planUsageEnabled) {
                        if (viewState.models.isEmpty()) loadModels()
                    } else {
                        sendJob?.cancel()
                    }
                }
                .launchIn(viewModelScope)
            viewModelScope.launch { loadDocumentAndHistory() }
        }

    // region Sign-in

    fun signIn(openBrowser: (Uri) -> Unit, newAccount: Boolean = false) {
        val requestConsent = (viewState.auth as? ChatGptAuthState.SignedIn)?.planUsageEnabled == false
        signInJob?.cancel()
        signInJob = viewModelScope.launch {
            updateState { copy(error = null, models = emptyList(), selectedModel = null) }
            try {
                authManager.signIn(
                    openBrowser = openBrowser,
                    newAccount = newAccount,
                    requestConsent = requestConsent,
                )
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Timber.e(e, "ReaderChatViewModel: sign-in failed")
                updateState { copy(error = e.message ?: "Sign-in failed.") }
            }
        }
    }

    fun cancelSignIn() {
        signInJob?.cancel()
    }

    fun signOut() {
        sendJob?.cancel()
        viewModelScope.launch {
            val revoked = authManager.signOut()
            updateState {
                copy(
                    models = emptyList(),
                    selectedModel = null,
                    error = if (revoked) null else "Signed out on this device, but ChatGPT didn't " +
                            "confirm the session was revoked. You can disconnect Zotero in ChatGPT settings."
                )
            }
        }
    }

    // endregion

    // region Composer

    fun selectModel(model: ChatGptModel) {
        credentialStore.selectedModel = model.slug
        updateState { copy(selectedModel = model, reasoningEffort = effortFor(model)) }
    }

    fun selectReasoningEffort(effort: String) {
        val model = viewState.selectedModel ?: return
        credentialStore.setReasoningEffort(model.slug, effort)
        updateState { copy(reasoningEffort = effort) }
    }

    fun setAutoAttachSelection(enabled: Boolean) {
        credentialStore.autoAttachSelection = enabled
        updateState { copy(autoAttachSelection = enabled) }
    }

    fun setInput(text: String) {
        updateState { copy(input = text) }
    }

    fun setQuote(quote: String?) {
        updateState { copy(quote = quote?.trim()?.takeIf { it.isNotEmpty() }) }
    }

    fun addImage(image: ChatImage) {
        updateState { copy(images = images + image) }
    }

    fun removeImage(image: ChatImage) {
        updateState { copy(images = images - image) }
    }

    fun runQuickAction(action: ChatQuickAction) {
        send(textOverride = action.prompt)
    }

    /** Ask for the last answer again, starting from intuition. */
    fun explainAgain() {
        send(
            textOverride = "Explain that again differently: start with the intuition in plain " +
                    "words (an analogy helps), then walk through the details step by step."
        )
    }

    // endregion

    /** Stops the answer being written, keeping what has arrived so far. */
    fun stop() {
        val partial = viewState.streamingText
        sendJob?.cancel()
        updateState {
            copy(
                isSending = false,
                phase = null,
                streamingText = null,
                turns = if (!partial.isNullOrBlank()) turns + ChatTurn("assistant", "$partial\n\n*(stopped)*") else turns,
            )
        }
    }

    // region Conversations

    fun newChat() {
        sendJob?.cancel()
        updateState {
            copy(
                turns = emptyList(), streamingText = null, noteKey = null, error = null,
                quote = null, images = emptyList(), savedAnswers = emptySet(), phase = null,
            )
        }
    }

    fun openChat(entry: ChatHistoryEntry) {
        sendJob?.cancel()
        viewModelScope.launch {
            val note = readChatNotes().firstOrNull { it.key == entry.noteKey } ?: return@launch
            updateState {
                copy(
                    turns = ChatNoteFormat.decode(note.html), noteKey = note.key,
                    streamingText = null, error = null, savedAnswers = emptySet(), phase = null,
                )
            }
        }
    }

    // endregion

    fun send(textOverride: String? = null) {
        val question = (textOverride ?: viewState.input).trim()
        val model = viewState.selectedModel
        if (model == null) {
            loadModels()
            return
        }
        if (question.isEmpty() || viewState.isSending) return
        val quote = viewState.quote
        val userText = if (quote != null) {
            "About this passage:\n\"$quote\"\n\n$question"
        } else {
            question
        }
        val turns = viewState.turns + ChatTurn("user", userText, viewState.images)
        updateState {
            copy(
                turns = turns,
                input = if (textOverride != null) input else "",
                quote = null,
                images = emptyList(),
                isSending = true,
                error = null,
            )
        }
        val effort = viewState.reasoningEffort.takeIf { model.reasoningEfforts.isNotEmpty() }
        sendJob = viewModelScope.launch {
            try {
                updateState { copy(phase = ChatPhase.ReadingDocument) }
                val context = contextProvider(true)
                val answer = StringBuilder()
                client.stream(
                    model = model.slug,
                    instructions = instructions,
                    context = documentContext(context),
                    turns = turns,
                    reasoningEffort = effort,
                ).collect { event ->
                    when (event) {
                        is ChatStreamEvent.Phase -> updateState {
                            copy(
                                phase = when (event.phase) {
                                    ChatStreamPhase.Thinking -> ChatPhase.Thinking
                                    ChatStreamPhase.Writing -> ChatPhase.Writing
                                },
                                streamingText = if (event.phase == ChatStreamPhase.Writing) "" else streamingText,
                            )
                        }

                        is ChatStreamEvent.Text -> {
                            answer.append(event.delta)
                            updateState { copy(streamingText = answer.toString()) }
                        }
                    }
                }
                val finalTurns = turns + ChatTurn("assistant", answer.toString())
                updateState { copy(turns = finalTurns, streamingText = null) }
                saveChat(context, finalTurns)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Timber.e(e, "ReaderChatViewModel: request failed")
                updateState { copy(error = e.message ?: "Request failed.", streamingText = null) }
            } finally {
                if (sendJob == coroutineContext[Job]) {
                    updateState { copy(isSending = false, phase = null) }
                }
            }
        }
    }

    /** Keeps one answer as its own note on the paper, with the question and passage it answered. */
    fun saveAnswerAsNote(index: Int) {
        val answer = viewState.turns.getOrNull(index)?.takeIf { it.role == "assistant" } ?: return
        val question = viewState.turns.subList(0, index).lastOrNull { it.role == "user" }
        val (passage, questionText) = splitQuestion(question?.text.orEmpty())
        viewModelScope.launch {
            val context = contextProvider(false)
            val parentKey = context.parentKey
            if (parentKey == null || !context.library.metadataEditable) {
                updateState { copy(error = "Notes can't be added to this document.") }
                return@launch
            }
            val html = ChatMarkdown.answerNote(
                question = questionText,
                passage = passage,
                answerMarkdown = answer.text,
                documentTitle = documentTitle,
                location = context.pageProgress?.substringBefore(" ("),
                modelName = viewState.selectedModel?.displayName,
            )
            try {
                withContext(dispatchers.io) {
                    dbWrapperMain.realmDbStorage.perform(
                        request = CreateNoteDbRequest(
                            note = Note(key = KeyGenerator.newKey(), text = html, tags = emptyList()),
                            localizedType = schemaController.localizedItemType(ItemTypes.note) ?: "",
                            libraryId = context.library.identifier,
                            collectionKey = null,
                            parentKey = parentKey,
                        ),
                        invalidateRealm = true
                    )
                }
                updateState { copy(savedAnswers = savedAnswers + index) }
            } catch (e: Throwable) {
                if (e is CancellationException) throw e
                Timber.e(e, "ReaderChatViewModel: can't save answer as note")
                updateState { copy(error = "The answer couldn't be saved as a note.") }
            }
        }
    }

    /** Separates the quoted passage from the question in a user message. */
    private fun splitQuestion(text: String): Pair<String?, String> {
        if (!text.startsWith("About this passage:\n\"")) return null to text
        val rest = text.removePrefix("About this passage:\n\"")
        val end = rest.indexOf("\"\n\n")
        if (end < 0) return null to text
        return rest.substring(0, end) to rest.substring(end + 3)
    }

    private fun effortFor(model: ChatGptModel): String? {
        if (model.reasoningEfforts.isEmpty()) return null
        return credentialStore.reasoningEffort(model.slug)?.takeIf { it in model.reasoningEfforts }
            ?: model.defaultReasoningEffort?.takeIf { it in model.reasoningEfforts }
            ?: model.reasoningEfforts.getOrNull(model.reasoningEfforts.size / 2)
    }

    private fun loadModels() {
        if (modelsJob?.isActive == true) return
        modelsJob = viewModelScope.launch {
            try {
                val models = client.models()
                val saved = credentialStore.selectedModel
                val selected = models.firstOrNull { it.slug == saved } ?: models.firstOrNull()
                updateState {
                    copy(
                        models = models,
                        selectedModel = selected,
                        reasoningEffort = selected?.let { effortFor(it) },
                        error = if (models.isEmpty()) "No ChatGPT models are available for this account." else error,
                    )
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Timber.e(e, "ReaderChatViewModel: can't load models")
                updateState { copy(error = e.message ?: "Couldn't load ChatGPT models.") }
            }
        }
    }

    /** Title, metadata and the most recent chat saved for this document (maybe on another device). */
    private suspend fun loadDocumentAndHistory() {
        val context = contextProvider(false)
        val key = context.parentKey ?: context.attachmentKey
        documentTitle = readOrDefault("") {
            read(ReadItemTitleDbRequest(key, context.library.identifier))
        }
        itemContext = readOrDefault("") {
            read(ReadChatItemContextDbRequest(key, context.library.identifier))
        }
        updateState { copy(canSave = context.parentKey != null && context.library.metadataEditable) }
        val notes = readChatNotes()
        updateState { copy(history = notes.map(::historyEntry)) }
        val latest = notes.firstOrNull() ?: return
        val turns = ChatNoteFormat.decode(latest.html)
        if (turns.isNotEmpty() && viewState.turns.isEmpty()) {
            updateState { copy(turns = turns, noteKey = latest.key) }
        }
    }

    fun refreshHistory() {
        viewModelScope.launch {
            val history = readChatNotes().map(::historyEntry)
            updateState { copy(history = history) }
        }
    }

    private fun historyEntry(note: ChatNoteSnapshot): ChatHistoryEntry {
        val firstQuestion = ChatNoteFormat.decode(note.html).firstOrNull { it.role == "user" }?.text
        val (_, question) = splitQuestion(firstQuestion.orEmpty())
        val title = question.lineSequence().firstOrNull { it.isNotBlank() }?.trim()
            ?.let { if (it.length > 60) it.take(57) + "…" else it }
            ?: "Untitled chat"
        return ChatHistoryEntry(note.key, title)
    }

    private suspend fun readChatNotes(): List<ChatNoteSnapshot> {
        val context = contextProvider(false)
        val parentKey = context.parentKey ?: return emptyList()
        return readOrDefault(emptyList()) {
            read(
                ReadChatNotesDbRequest(
                    parentKey = parentKey,
                    attachmentKey = context.attachmentKey,
                    libraryId = context.library.identifier
                )
            )
        }
    }

    private suspend inline fun <T> readOrDefault(
        default: T,
        crossinline block: org.zotero.android.database.RealmDbStorage.() -> T,
    ): T = withContext(dispatchers.io) {
        try {
            dbWrapperMain.realmDbStorage.block()
        } catch (e: Throwable) {
            if (e is CancellationException) throw e
            Timber.e(e, "ReaderChatViewModel: database read failed")
            default
        }
    }

    /** Reads on this IO thread and releases its Realm instance afterwards. */
    private inline fun <reified T : Any> org.zotero.android.database.RealmDbStorage.read(
        request: org.zotero.android.database.DbResponseRequest<T>
    ): T = perform(request = request, invalidateRealm = true)

    private suspend fun saveChat(context: ReaderChatContext, turns: List<ChatTurn>) {
        val parentKey = context.parentKey ?: return
        if (!context.library.metadataEditable) return
        val existingKey = viewState.noteKey
        val noteKey = existingKey ?: KeyGenerator.newKey()
        val note = Note(
            key = noteKey,
            text = ChatNoteFormat.encode(documentTitle, context.attachmentKey, turns),
            tags = emptyList()
        )
        val libraryId = context.library.identifier
        try {
            withContext(dispatchers.io) {
                if (existingKey != null) {
                    dbWrapperMain.realmDbStorage.perform(EditNoteDbRequest(note, libraryId))
                } else {
                    dbWrapperMain.realmDbStorage.perform(
                        request = CreateNoteDbRequest(
                            note = note,
                            localizedType = schemaController.localizedItemType(ItemTypes.note)
                                ?: "",
                            libraryId = libraryId,
                            collectionKey = null,
                            parentKey = parentKey,
                        ),
                        invalidateRealm = true
                    )
                }
            }
            updateState { copy(noteKey = noteKey) }
            if (existingKey == null) refreshHistory()
        } catch (e: Throwable) {
            if (e is CancellationException) throw e
            Timber.e(e, "ReaderChatViewModel: can't save chat note")
            updateState { copy(error = "The conversation couldn't be saved to a note.") }
        }
    }

    private fun documentContext(context: ReaderChatContext): String {
        val text = context.documentText
        return buildString {
            append("The user is reading this item in Zotero:\n")
            append(itemContext.ifBlank { "Title: $documentTitle" }).append("\n")
            if (text.isNullOrBlank()) {
                append("\nThe document's text isn't available, so answer from the conversation and any images.\n")
            } else {
                val truncated = text.length > maxDocumentChars
                append("\nFull text of the document")
                if (truncated) append(" (truncated)")
                append(":\n<document>\n").append(text.take(maxDocumentChars)).append("\n</document>\n")
            }
            // Kept after the document so the long, stable prefix can be cached between requests.
            context.pageProgress?.let { append("\nThey are currently at ").append(it).append(".") }
        }
    }

    private companion object {
        const val maxDocumentChars = 200_000
        const val instructions =
            "You are a reading assistant inside Zotero, helping a researcher understand the " +
                    "document they are reading. Ground answers in the document text and any " +
                    "images you're given, and say when something isn't in them. Point to the " +
                    "section, figure or page when you can. Use Markdown: short paragraphs, " +
                    "lists, **bold** for key terms, tables when comparing; write math in LaTeX " +
                    "between $ signs. Be concise."
    }
}

internal enum class ChatPhase { ReadingDocument, Thinking, Writing }

internal data class ReaderChatViewState(
    val auth: ChatGptAuthState = ChatGptAuthState.SignedOut,
    val models: List<ChatGptModel> = emptyList(),
    val selectedModel: ChatGptModel? = null,
    val reasoningEffort: String? = null,
    val turns: List<ChatTurn> = emptyList(),
    val streamingText: String? = null,
    val input: String = "",
    val quote: String? = null,
    val images: List<ChatImage> = emptyList(),
    val isSending: Boolean = false,
    val phase: ChatPhase? = null,
    val noteKey: String? = null,
    val canSave: Boolean = true,
    val savedAnswers: Set<Int> = emptySet(),
    val history: List<ChatHistoryEntry> = emptyList(),
    val autoAttachSelection: Boolean = true,
    val error: String? = null,
) : ViewState

internal sealed class ReaderChatViewEffect : ViewEffect
