package org.zotero.android.ai.ui

import android.net.Uri
import androidx.lifecycle.viewModelScope
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
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
import org.zotero.android.ai.tools.ZoteroToolSpec
import org.zotero.android.ai.tools.ZoteroTools
import org.zotero.android.architecture.BaseViewModel2
import org.zotero.android.architecture.ViewEffect
import org.zotero.android.architecture.ViewState
import org.zotero.android.architecture.coroutines.Dispatchers
import org.zotero.android.database.DbResponseRequest
import org.zotero.android.database.DbWrapperMain
import org.zotero.android.database.RealmDbStorage
import org.zotero.android.database.objects.ItemTypes
import org.zotero.android.database.objects.RTypedTag
import org.zotero.android.database.requests.ChatNoteSnapshot
import org.zotero.android.database.requests.CreateNoteDbRequest
import org.zotero.android.database.requests.EditNoteDbRequest
import org.zotero.android.database.requests.ReadChatItemContextDbRequest
import org.zotero.android.database.requests.ReadChatNotesDbRequest
import org.zotero.android.database.requests.ReadItemTitleDbRequest
import org.zotero.android.database.requests.ReadLibraryChatNotesDbRequest
import org.zotero.android.database.requests.NoteHtml
import org.zotero.android.database.requests.ReadNoteHtmlDbRequest
import org.zotero.android.screens.reader.ReaderChatContext
import org.zotero.android.sync.KeyGenerator
import org.zotero.android.sync.Library
import org.zotero.android.sync.Note
import org.zotero.android.sync.SchemaController
import org.zotero.android.sync.Tag
import timber.log.Timber
import javax.inject.Inject

/** Where a chat lives: next to an open document, or over the whole library. */
sealed interface AiChatScope {
    class Reader(val provider: suspend (includeDocumentText: Boolean) -> ReaderChatContext) : AiChatScope
    class Library(val provider: () -> LibraryChatContext) : AiChatScope
}

/** What the library panel knows about where the user is. */
data class LibraryChatContext(
    val library: Library,
    val collectionName: String?,
    val collectionKey: String?,
    val selectedKeys: List<String>,
) {
    companion object {
        /** The view named to the model while a group library is shown (same text on desktop). */
        const val groupView = "a group library (the tools only reach My Library)"
    }
}

/** A saved conversation, for the history menu. */
internal data class ChatHistoryEntry(val noteKey: String, val title: String)

/** A one-tap prompt for the attached selection. */
internal enum class ChatQuickAction(val label: String, val prompt: String) {
    Explain("Explain", "Explain this passage in the context of the paper."),
    Simplify("Simplify", "Explain this passage in simple terms, as if to a smart newcomer to the field."),
    Translate("Translate", "Translate this passage into English (or, if it is English, into plain everyday English) and briefly note any terms that don't translate cleanly."),
    Define("Define terms", "Define the technical terms and symbols in this passage, as they're used in this paper."),
}

/** Starter prompts for an empty library chat. */
internal val librarySuggestions = listOf(
    "What did I add recently?",
    "Which papers in this collection have no tags? Suggest tags for them.",
    "Summarize the papers I've selected.",
    "Find my papers about reinforcement learning.",
)

/** A write the model wants to make, waiting for the user. */
internal data class PendingApproval(val toolName: String, val description: String)

@HiltViewModel
internal class AiChatViewModel @Inject constructor(
    private val authManager: ChatGptAuthManager,
    private val credentialStore: ChatGptCredentialStore,
    private val client: ChatGptResponsesClient,
    private val toolSpec: ZoteroToolSpec,
    private val tools: ZoteroTools,
    private val dbWrapperMain: DbWrapperMain,
    private val schemaController: SchemaController,
    private val dispatchers: Dispatchers,
) : BaseViewModel2<AiChatViewState, AiChatViewEffect>(AiChatViewState()) {

    private lateinit var scope: AiChatScope
    private var sendJob: Job? = null
    private var signInJob: Job? = null
    private var modelsJob: Job? = null
    private var approval: CompletableDeferred<Boolean>? = null

    /** The chat's note as this screen last loaded or wrote it, to notice changes from other devices. */
    private var noteHtml: String? = null
    private var documentTitle: String = ""
    private var itemContext: String = ""

    fun start(scope: AiChatScope) = initOnce {
        this.scope = scope
        updateState {
            copy(
                isLibrary = scope is AiChatScope.Library,
                autoAttachSelection = credentialStore.autoAttachSelection,
                autoApprove = credentialStore.autoApproveChanges,
            )
        }
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
                Timber.e(e, "AiChatViewModel: sign-in failed")
                updateState { copy(error = e.message ?: "Sign-in failed.") }
            }
        }
    }

    fun cancelSignIn() {
        signInJob?.cancel()
    }

    fun signOut() {
        stop()
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

    fun setAutoApprove(enabled: Boolean) {
        credentialStore.autoApproveChanges = enabled
        updateState { copy(autoApprove = enabled) }
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

    fun runSuggestion(prompt: String) {
        send(textOverride = prompt)
    }

    /** Ask for the last answer again, starting from intuition. */
    fun explainAgain() {
        send(
            textOverride = "Explain that again differently: start with the intuition in plain " +
                    "words (an analogy helps), then walk through the details step by step."
        )
    }

    // endregion

    // region Approval

    fun approvePending() {
        approval?.complete(true)
    }

    fun declinePending() {
        approval?.complete(false)
    }

    private suspend fun askApproval(name: String, description: String): Boolean {
        // Read the setting now (another screen may have changed it); the spec decides what always asks.
        if (!toolSpec.shouldAsk(name, credentialStore.autoApproveChanges)) return true
        val deferred = CompletableDeferred<Boolean>()
        approval = deferred
        updateState { copy(pendingApproval = PendingApproval(name, description)) }
        return try {
            deferred.await()
        } finally {
            approval = null
            updateState { copy(pendingApproval = null) }
        }
    }

    // endregion

    // region Conversations

    /** Stops the answer being written, keeping what has arrived so far. */
    fun stop() {
        val partial = viewState.streamingText
        sendJob?.cancel()
        approval?.complete(false)
        updateState {
            copy(
                isSending = false,
                phase = null,
                streamingText = null,
                turns = if (!partial.isNullOrBlank()) turns + ChatTurn("assistant", "$partial\n\n*(stopped)*") else turns,
            )
        }
    }

    fun newChat() {
        sendJob?.cancel()
        approval?.complete(false)
        noteHtml = null
        updateState {
            copy(
                turns = emptyList(), streamingText = null, noteKey = null, error = null,
                quote = null, images = emptyList(), savedAnswers = emptySet(), phase = null,
                liveActivity = emptyList(),
            )
        }
        rememberLibraryChat()
    }

    fun openChat(entry: ChatHistoryEntry) {
        sendJob?.cancel()
        viewModelScope.launch {
            val note = readChatNotes().firstOrNull { it.key == entry.noteKey } ?: return@launch
            noteHtml = note.html
            updateState {
                copy(
                    turns = ChatNoteFormat.decode(note.html), noteKey = note.key,
                    streamingText = null, error = null, savedAnswers = emptySet(), phase = null,
                )
            }
            rememberLibraryChat()
        }
    }

    /** Keeps the library conversation across a fold/unfold (LibraryAiSessionRestore). */
    private fun rememberLibraryChat() {
        if (scope is AiChatScope.Library) LibraryAiSessionRestore.noteKey = viewState.noteKey
    }

    fun refreshHistory() {
        viewModelScope.launch {
            val history = readChatNotes().map(::historyEntry)
            updateState { copy(history = history) }
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
        val userText = if (quote != null) "About this passage:\n\"$quote\"\n\n$question" else question
        val userTurn = ChatTurn("user", userText, viewState.images)
        updateState {
            copy(
                input = if (textOverride != null) input else "",
                quote = null,
                images = emptyList(),
                isSending = true,
                error = null,
                liveActivity = emptyList(),
            )
        }
        val effort = viewState.reasoningEffort.takeIf { model.reasoningEfforts.isNotEmpty() }
        sendJob = viewModelScope.launch {
            try {
                // Continue the conversation as it is now, including turns synced from another device.
                syncWithNote()
                val turns = viewState.turns + userTurn
                updateState { copy(turns = turns) }
                updateState { copy(phase = if (viewState.isLibrary) ChatPhase.Thinking else ChatPhase.ReadingDocument) }
                val context = buildContext()
                val answer = runTurn(model.slug, effort, context, turns)
                val finalTurns = turns + answer
                updateState { copy(turns = finalTurns, streamingText = null, liveActivity = emptyList()) }
                saveChat(finalTurns)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Timber.e(e, "AiChatViewModel: request failed")
                updateState { copy(error = e.message ?: "Request failed.", streamingText = null) }
            } finally {
                if (sendJob == coroutineContext[Job]) {
                    updateState { copy(isSending = false, phase = null) }
                }
            }
        }
    }

    /** Reloads the chat from its note if another device (or the note editor) changed it. */
    private suspend fun syncWithNote() {
        val key = viewState.noteKey ?: return
        val html = readNoteHtml(key)
        if (html == null) {
            noteHtml = null
            updateState { copy(noteKey = null) }
        } else if (html != noteHtml) {
            noteHtml = html
            updateState { copy(turns = ChatNoteFormat.decode(html), savedAnswers = emptySet()) }
        }
    }

    private suspend fun readNoteHtml(key: String): String? {
        val libraryId = when (val scope = scope) {
            is AiChatScope.Reader -> scope.provider(false).library.identifier
            is AiChatScope.Library -> scope.provider().library.identifier
        }
        return readOrDefault(NoteHtml(null)) { read(ReadNoteHtmlDbRequest(key, libraryId)) }.html
    }

    /**
     * One user turn: ask the model, run the tools it calls (asking for approval before writes),
     * feed the results back, and repeat until it answers. See ai/spec/chat.md.
     */
    private suspend fun runTurn(
        model: String,
        effort: String?,
        context: String,
        turns: List<ChatTurn>,
    ): ChatTurn {
        val input = JsonArray()
        input.add(client.developerItem(context))
        turns.forEach { turn ->
            if (turn.role == "assistant") turn.trace.forEach { input.add(it.deepCopy()) }
            input.add(client.inputItem(turn))
        }
        val instructions = if (viewState.isLibrary) toolSpec.libraryInstructions else toolSpec.readerInstructions
        val trace = mutableListOf<JsonObject>()
        val activity = mutableListOf<String>()
        val answer = StringBuilder()
        var rounds = 0
        while (true) {
            val calls = mutableListOf<JsonObject>()
            val roundText = StringBuilder()
            client.stream(model, instructions, input, toolSpec.apiTools, effort).collect { event ->
                when (event) {
                    is ChatStreamEvent.Phase -> updateState {
                        copy(
                            phase = when (event.phase) {
                                ChatStreamPhase.Thinking -> ChatPhase.Thinking
                                ChatStreamPhase.Writing -> ChatPhase.Writing
                            },
                            streamingText = streamingText ?: "",
                        )
                    }

                    is ChatStreamEvent.Text -> {
                        roundText.append(event.delta)
                        val shown = joinAnswer(answer, roundText)
                        updateState { copy(streamingText = shown) }
                    }

                    is ChatStreamEvent.ItemDone -> {
                        if (event.item.get("type")?.asString == "function_call") calls += event.item
                    }
                }
            }
            if (roundText.isNotBlank()) {
                if (answer.isNotEmpty()) answer.append("\n\n")
                answer.append(roundText.toString().trim())
            }
            if (calls.isEmpty()) break
            if (++rounds > maxToolRounds) {
                answer.append("\n\n*(Stopped: too many tool calls in one answer.)*")
                break
            }
            if (roundText.isNotBlank()) {
                val message = JsonObject().apply {
                    addProperty("role", "assistant")
                    addProperty("content", roundText.toString())
                }
                input.add(message)
                trace += message
            }
            for (call in calls) {
                val name = call.get("name")?.asString.orEmpty()
                val callId = call.get("call_id")?.asString.orEmpty()
                val argumentsText = call.get("arguments")?.asString ?: "{}"
                // Items aren't stored server-side (store=false), so send calls back without ids.
                val functionCall = JsonObject().apply {
                    addProperty("type", "function_call")
                    addProperty("call_id", callId)
                    addProperty("name", name)
                    addProperty("arguments", argumentsText)
                }
                input.add(functionCall)
                trace += functionCall
                val args = runCatching { JsonParser.parseString(argumentsText).asJsonObject }
                    .getOrElse { JsonObject() }
                updateState { copy(phase = ChatPhase.RunningTool(toolLabel(name))) }
                val result = if (!toolSpec.isKnown(name)) {
                    errorResult("invalid_argument", "Unknown tool $name")
                } else if (toolSpec.isWrite(name)) {
                    val description = tools.describeWrite(name, args)
                    if (askApproval(name, description)) {
                        tools.execute(name, args).also {
                            activity += (if (it.has("error")) "✗ " else "✓ ") + description
                        }
                    } else {
                        activity += "Declined: $description"
                        errorResult("declined", "The user declined this change.")
                    }
                } else {
                    tools.execute(name, args).also {
                        activity += toolLabel(name) + (if (it.has("error")) " (failed)" else "")
                    }
                }
                updateState { copy(liveActivity = activity.toList()) }
                val output = JsonObject().apply {
                    addProperty("type", "function_call_output")
                    addProperty("call_id", callId)
                    addProperty("output", result.toString())
                }
                input.add(output)
                trace += output
            }
            updateState { copy(phase = ChatPhase.Thinking) }
        }
        return ChatTurn("assistant", answer.toString(), trace = trace, activity = activity)
    }

    private fun joinAnswer(answer: StringBuilder, roundText: StringBuilder): String =
        if (answer.isEmpty()) roundText.toString() else "$answer\n\n$roundText"

    private fun errorResult(code: String, message: String) = JsonObject().apply {
        addProperty("error", code)
        addProperty("message", message)
    }

    /** Keeps one answer as its own note, with the question and passage it answered. */
    fun saveAnswerAsNote(index: Int) {
        val answer = viewState.turns.getOrNull(index)?.takeIf { it.role == "assistant" } ?: return
        val question = viewState.turns.subList(0, index).lastOrNull { it.role == "user" }
        val (passage, questionText) = splitQuestion(question?.text.orEmpty())
        viewModelScope.launch {
            val target = noteTarget()
            if (target == null) {
                updateState { copy(error = "Notes can't be added here.") }
                return@launch
            }
            val html = ChatMarkdown.answerNote(
                question = questionText,
                passage = passage,
                answerMarkdown = answer.text,
                documentTitle = documentTitle.ifBlank { "your library" },
                location = target.location,
                modelName = viewState.selectedModel?.displayName,
            )
            try {
                withContext(dispatchers.io) {
                    dbWrapperMain.realmDbStorage.perform(
                        request = CreateNoteDbRequest(
                            note = Note(key = KeyGenerator.newKey(), text = html, tags = emptyList()),
                            localizedType = schemaController.localizedItemType(ItemTypes.note) ?: "",
                            libraryId = target.library.identifier,
                            collectionKey = target.collectionKey,
                            parentKey = target.parentKey,
                        ),
                        invalidateRealm = true
                    )
                }
                updateState { copy(savedAnswers = savedAnswers + index) }
            } catch (e: Throwable) {
                if (e is CancellationException) throw e
                Timber.e(e, "AiChatViewModel: can't save answer as note")
                updateState { copy(error = "The answer couldn't be saved as a note.") }
            }
        }
    }

    private data class NoteTarget(
        val library: Library,
        val parentKey: String?,
        val collectionKey: String?,
        val location: String?,
    )

    private suspend fun noteTarget(): NoteTarget? = when (val scope = scope) {
        is AiChatScope.Reader -> {
            val context = scope.provider(false)
            val parentKey = context.parentKey
            if (parentKey == null || !context.library.metadataEditable) null
            else NoteTarget(context.library, parentKey, null, context.pageProgress?.substringBefore(" ("))
        }

        is AiChatScope.Library -> {
            val context = scope.provider()
            if (!context.library.metadataEditable) null
            else NoteTarget(context.library, null, context.collectionKey, null)
        }
    }

    /** Separates the quoted passage from the question in a user message. */
    private fun splitQuestion(text: String): Pair<String?, String> {
        // Blank lines are lost when a conversation comes back from a note, so allow any newlines.
        val match = Regex("^About this passage:\\n\"(.*?)\"\\n+(.*)$", RegexOption.DOT_MATCHES_ALL)
            .find(text) ?: return null to text
        return match.groupValues[1] to match.groupValues[2]
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
                Timber.e(e, "AiChatViewModel: can't load models")
                updateState { copy(error = e.message ?: "Couldn't load ChatGPT models.") }
            }
        }
    }

    /** Title, metadata and the most recent conversation (possibly from another device). */
    private suspend fun loadDocumentAndHistory() {
        when (val scope = scope) {
            is AiChatScope.Reader -> {
                val context = scope.provider(false)
                val key = context.parentKey ?: context.attachmentKey
                documentTitle = readOrDefault("") {
                    read(ReadItemTitleDbRequest(key, context.library.identifier))
                }
                itemContext = readOrDefault("") {
                    read(ReadChatItemContextDbRequest(key, context.library.identifier))
                }
                updateState { copy(canSave = context.parentKey != null && context.library.metadataEditable) }
            }

            is AiChatScope.Library -> {
                updateState { copy(canSave = scope.provider().library.metadataEditable) }
            }
        }
        val notes = readChatNotes()
        updateState { copy(history = notes.map(::historyEntry)) }
        // Library chats start fresh, unless a fold/unfold just recreated the screen mid-conversation.
        if (scope is AiChatScope.Library) {
            val key = LibraryAiSessionRestore.noteKey ?: return
            val note = notes.firstOrNull { it.key == key } ?: return
            if (viewState.turns.isEmpty()) {
                noteHtml = note.html
                updateState { copy(turns = ChatNoteFormat.decode(note.html), noteKey = note.key) }
            }
            return
        }
        // Reader chats pick up where they left off.
        if (scope is AiChatScope.Reader) {
            val latest = notes.firstOrNull() ?: return
            val turns = ChatNoteFormat.decode(latest.html)
            if (turns.isNotEmpty() && viewState.turns.isEmpty()) {
                noteHtml = latest.html
                updateState { copy(turns = turns, noteKey = latest.key) }
            }
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

    private suspend fun readChatNotes(): List<ChatNoteSnapshot> = when (val scope = scope) {
        is AiChatScope.Reader -> {
            val context = scope.provider(false)
            val parentKey = context.parentKey
            if (parentKey == null) emptyList() else readOrDefault(emptyList()) {
                read(ReadChatNotesDbRequest(parentKey, context.attachmentKey, context.library.identifier))
            }
        }

        is AiChatScope.Library -> {
            val libraryId = scope.provider().library.identifier
            readOrDefault(emptyList()) { read(ReadLibraryChatNotesDbRequest(libraryId)) }
        }
    }

    private suspend inline fun <T> readOrDefault(
        default: T,
        crossinline block: RealmDbStorage.() -> T,
    ): T = withContext(dispatchers.io) {
        try {
            dbWrapperMain.realmDbStorage.block()
        } catch (e: Throwable) {
            if (e is CancellationException) throw e
            Timber.e(e, "AiChatViewModel: database read failed")
            default
        }
    }

    /** Reads on this IO thread, seeing the latest data, and releases its Realm instance afterwards. */
    private inline fun <reified T : Any> RealmDbStorage.read(request: DbResponseRequest<T>): T =
        perform(request = request, invalidateRealm = true, refreshRealm = true)

    private suspend fun saveChat(turns: List<ChatTurn>) {
        val (library, parentKey, html, tags) = when (val scope = scope) {
            is AiChatScope.Reader -> {
                val context = scope.provider(false)
                val parentKey = context.parentKey ?: return
                if (!context.library.metadataEditable) return
                SaveSpec(context.library, parentKey, ChatNoteFormat.encode(documentTitle, context.attachmentKey, turns), emptyList())
            }

            is AiChatScope.Library -> {
                val context = scope.provider()
                if (!context.library.metadataEditable) return
                SaveSpec(
                    context.library, null, ChatNoteFormat.encodeLibrary(turns),
                    listOf(Tag(name = ChatNoteFormat.libraryChatTag, color = "", type = RTypedTag.Kind.manual))
                )
            }
        }
        // If the note changed on another device while we were answering, keep both versions.
        val existingKey = viewState.noteKey?.takeIf { readNoteHtml(it).let { current -> current != null && current == noteHtml } }
        val noteKey = existingKey ?: KeyGenerator.newKey()
        val note = Note(key = noteKey, text = html, tags = tags)
        try {
            withContext(dispatchers.io) {
                if (existingKey != null) {
                    dbWrapperMain.realmDbStorage.perform(EditNoteDbRequest(note, library.identifier))
                } else {
                    dbWrapperMain.realmDbStorage.perform(
                        request = CreateNoteDbRequest(
                            note = note,
                            localizedType = schemaController.localizedItemType(ItemTypes.note) ?: "",
                            libraryId = library.identifier,
                            collectionKey = null,
                            parentKey = parentKey,
                        ),
                        invalidateRealm = true
                    )
                }
            }
            noteHtml = html
            updateState { copy(noteKey = noteKey) }
            rememberLibraryChat()
            if (existingKey == null) refreshHistory()
        } catch (e: Throwable) {
            if (e is CancellationException) throw e
            Timber.e(e, "AiChatViewModel: can't save chat note")
            updateState { copy(error = "The conversation couldn't be saved to a note.") }
        }
    }

    private data class SaveSpec(val library: Library, val parentKey: String?, val html: String, val tags: List<Tag>)

    /** The context block (ai/spec/chat.md) for the current scope. */
    private suspend fun buildContext(): String = when (val scope = scope) {
        is AiChatScope.Reader -> {
            val context = scope.provider(true)
            val text = context.documentText
            buildString {
                append("The user is reading this item in Zotero:\n")
                append(itemContext.ifBlank { "Title: $documentTitle" }).append("\n")
                append("Item key: ").append(context.parentKey ?: context.attachmentKey).append("\n")
                if (text.isNullOrBlank()) {
                    append("\nThe document's text isn't available, so answer from the conversation and any images.\n")
                } else {
                    val truncated = text.length > maxDocumentChars
                    append("\nFull text of the document")
                    if (truncated) append(" (truncated)")
                    // The document can't close its own wrapper (ai/spec/chat.md).
                    val body = text.take(maxDocumentChars).replace(documentTag, "<$1 document>")
                    append(":\n<document>\n").append(body).append("\n</document>\n")
                }
                // Kept after the document so the long, stable prefix can be cached between requests.
                context.pageProgress?.let { append("\nThey are currently at ").append(it).append(".") }
            }
        }

        is AiChatScope.Library -> {
            val context = scope.provider()
            val selected = context.selectedKeys.take(50)
            val titles = if (selected.isEmpty()) emptyMap() else readOrDefault(emptyMap()) {
                selected.associateWith { key ->
                    runCatching { read(ReadItemTitleDbRequest(key, context.library.identifier)) }.getOrDefault("")
                }
            }
            buildString {
                append("The user is browsing their Zotero library.\n")
                if (context.collectionKey != null) {
                    append("Current collection: ").append(context.collectionName).append(" (")
                        .append(context.collectionKey).append(")\n")
                } else if (context.collectionName != null) {
                    append("Current view: ").append(context.collectionName).append("\n")
                }
                if (selected.isNotEmpty()) {
                    append("Selected items: ")
                    append(selected.joinToString(", ") { key ->
                        titles[key]?.takeIf { it.isNotBlank() }?.let { "$key “$it”" } ?: key
                    })
                    append("\n")
                }
            }
        }
    }

    private fun toolLabel(name: String): String = when (name) {
        "zotero_search_items" -> "Searching your library"
        "zotero_get_item_metadata" -> "Reading item details"
        "zotero_get_item_fulltext" -> "Reading a paper"
        "zotero_get_collections" -> "Looking at your collections"
        "zotero_get_collection_items" -> "Listing a collection"
        "zotero_get_tags" -> "Looking at your tags"
        "zotero_get_recent" -> "Checking recent additions"
        "zotero_get_notes" -> "Reading notes"
        "zotero_get_annotations" -> "Reading annotations"
        "zotero_batch_update_tags" -> "Updating tags"
        "zotero_set_item_collections" -> "Updating collections"
        "zotero_create_collection" -> "Creating a collection"
        "zotero_create_note" -> "Creating a note"
        "zotero_add_item_by_identifier" -> "Adding an item"
        "zotero_trash_items" -> "Moving items to the trash"
        else -> name
    }

    private companion object {
        const val maxDocumentChars = 200_000
        const val maxToolRounds = 12

        val documentTag = Regex("<(/?)document>", RegexOption.IGNORE_CASE)
    }
}

internal sealed interface ChatPhase {
    object ReadingDocument : ChatPhase
    object Thinking : ChatPhase
    object Writing : ChatPhase
    data class RunningTool(val label: String) : ChatPhase
}

internal data class AiChatViewState(
    val isLibrary: Boolean = false,
    val auth: ChatGptAuthState = ChatGptAuthState.SignedOut,
    val models: List<ChatGptModel> = emptyList(),
    val selectedModel: ChatGptModel? = null,
    val reasoningEffort: String? = null,
    val turns: List<ChatTurn> = emptyList(),
    val streamingText: String? = null,
    val liveActivity: List<String> = emptyList(),
    val input: String = "",
    val quote: String? = null,
    val images: List<ChatImage> = emptyList(),
    val isSending: Boolean = false,
    val phase: ChatPhase? = null,
    val pendingApproval: PendingApproval? = null,
    val noteKey: String? = null,
    val canSave: Boolean = true,
    val savedAnswers: Set<Int> = emptySet(),
    val history: List<ChatHistoryEntry> = emptyList(),
    val autoAttachSelection: Boolean = true,
    val autoApprove: Boolean = false,
    val error: String? = null,
) : ViewState

internal sealed class AiChatViewEffect : ViewEffect
