package org.zotero.android.ai.ui

import org.zotero.android.ai.chat.ChatMarkdown
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.Base64
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Crop
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Lightbulb
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.NoteAdd
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.outlined.ChatBubbleOutline
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.livedata.observeAsState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.mikepenz.markdown.m3.Markdown
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull
import com.mikepenz.markdown.m3.markdownTypography
import org.zotero.android.ai.api.ChatImage
import org.zotero.android.ai.api.ChatTurn
import org.zotero.android.ai.auth.ChatGptAuthConfig
import org.zotero.android.ai.auth.ChatGptAuthState
import org.zotero.android.uicomponents.Strings

/** What the reader shares with the chat: selection, images and page capture. */
internal class ReaderChatHooks(
    val quote: String?,
    val consumeQuote: () -> Unit,
    val image: ChatImage?,
    val consumeImage: () -> Unit,
    val selectedText: String?,
    val attachPage: () -> Unit,
    val selectArea: () -> Unit,
)

/**
 * The AI chat panel: next to (or under) an open document, or beside the library. In the reader it
 * follows what's happening there: selected text becomes a "Text selection" attachment, and pages
 * or areas of the page can be sent as images.
 */
@Composable
internal fun AiChatPanel(
    scope: AiChatScope,
    readerHooks: ReaderChatHooks?,
    onClose: () -> Unit,
    viewModel: AiChatViewModel = hiltViewModel(),
) {
    viewModel.start(scope)
    val viewState by viewModel.viewStates.observeAsState(AiChatViewState())

    if (readerHooks != null) {
        LaunchedEffect(readerHooks.quote) {
            if (readerHooks.quote != null) {
                viewModel.setQuote(readerHooks.quote)
                readerHooks.consumeQuote()
            }
        }
        LaunchedEffect(readerHooks.image) {
            if (readerHooks.image != null) {
                viewModel.addImage(readerHooks.image)
                readerHooks.consumeImage()
            }
        }
        LaunchedEffect(readerHooks.selectedText, viewState.autoAttachSelection) {
            if (readerHooks.selectedText != null && viewState.autoAttachSelection) {
                viewModel.setQuote(readerHooks.selectedText)
            }
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
    ) {
        PanelHeader(
            viewState = viewState,
            viewModel = viewModel,
            onClose = onClose,
        )
        HorizontalDivider()
        when (val auth = viewState.auth) {
            ChatGptAuthState.SignedOut, ChatGptAuthState.SigningIn -> {
                SignInPrompt(
                    isSigningIn = auth == ChatGptAuthState.SigningIn,
                    error = viewState.error,
                    onSignIn = { viewModel.signIn(it) },
                    onCancel = viewModel::cancelSignIn,
                )
            }

            is ChatGptAuthState.SignedIn -> {
                if (!auth.planUsageEnabled) {
                    SignInPrompt(
                        isSigningIn = false,
                        error = viewState.error
                            ?: stringResource(Strings.reader_chat_plan_usage_disabled),
                        onSignIn = { viewModel.signIn(it) },
                        onCancel = viewModel::cancelSignIn,
                        onSignOut = viewModel::signOut,
                    )
                } else {
                    ChatMessages(
                        viewState = viewState,
                        viewModel = viewModel,
                        modifier = Modifier.weight(1f),
                    )
                    viewState.pendingApproval?.let { pending ->
                        ApprovalCard(
                            pending = pending,
                            onApprove = viewModel::approvePending,
                            onDecline = viewModel::declinePending,
                        )
                    }
                    Composer(
                        viewState = viewState,
                        viewModel = viewModel,
                        onAttachPage = readerHooks?.attachPage,
                        onSelectArea = readerHooks?.selectArea,
                    )
                }
            }
        }
    }
}

@Composable
private fun ApprovalCard(pending: PendingApproval, onApprove: () -> Unit, onDecline: () -> Unit) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.tertiaryContainer,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 4.dp),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(
                text = stringResource(Strings.reader_chat_approval_title),
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onTertiaryContainer,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                text = pending.description,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onTertiaryContainer,
            )
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                horizontalArrangement = Arrangement.End,
            ) {
                TextButton(onClick = onDecline) { Text(stringResource(Strings.reader_chat_decline)) }
                Spacer(Modifier.width(8.dp))
                Button(onClick = onApprove) { Text(stringResource(Strings.reader_chat_approve)) }
            }
        }
    }
}

private fun openInBrowser(context: Context, uri: Uri) {
    context.startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}

@Composable
private fun PanelHeader(
    viewState: AiChatViewState,
    viewModel: AiChatViewModel,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    var showHistory by remember { mutableStateOf(false) }
    var showMenu by remember { mutableStateOf(false) }
    val signedIn = viewState.auth as? ChatGptAuthState.SignedIn
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 12.dp, end = 4.dp, top = 2.dp, bottom = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            Icons.Default.AutoAwesome,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(20.dp),
        )
        Spacer(Modifier.width(8.dp))
        Text(
            text = stringResource(
                if (viewState.isLibrary) Strings.reader_chat_library_title else Strings.reader_chat_title
            ),
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.weight(1f),
        )
        if (signedIn?.planUsageEnabled == true) {
            Box {
                IconButton(onClick = {
                    viewModel.refreshHistory()
                    showHistory = true
                }) {
                    Icon(Icons.Default.History, contentDescription = stringResource(Strings.reader_chat_history))
                }
                DropdownMenu(expanded = showHistory, onDismissRequest = { showHistory = false }) {
                    if (viewState.history.isEmpty()) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Strings.reader_chat_history_empty)) },
                            onClick = {},
                            enabled = false,
                        )
                    }
                    viewState.history.forEach { entry ->
                        DropdownMenuItem(
                            text = { Text(entry.title, maxLines = 2, overflow = TextOverflow.Ellipsis) },
                            leadingIcon = {
                                if (entry.noteKey == viewState.noteKey) {
                                    Icon(Icons.Default.Check, contentDescription = null)
                                } else {
                                    Icon(Icons.Outlined.ChatBubbleOutline, contentDescription = null)
                                }
                            },
                            onClick = {
                                showHistory = false
                                viewModel.openChat(entry)
                            }
                        )
                    }
                }
            }
            IconButton(onClick = viewModel::newChat) {
                Icon(Icons.Default.Add, contentDescription = stringResource(Strings.reader_chat_new_chat))
            }
            Box {
                IconButton(onClick = { showMenu = true }) {
                    Icon(Icons.Default.MoreVert, contentDescription = null)
                }
                DropdownMenu(expanded = showMenu, onDismissRequest = { showMenu = false }) {
                    signedIn.email?.let { email ->
                        DropdownMenuItem(text = { Text(email) }, onClick = {}, enabled = false)
                    }
                    if (!viewState.isLibrary) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Strings.reader_chat_auto_attach)) },
                            trailingIcon = {
                                Checkbox(checked = viewState.autoAttachSelection, onCheckedChange = null)
                            },
                            onClick = { viewModel.setAutoAttachSelection(!viewState.autoAttachSelection) }
                        )
                    }
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_auto_approve)) },
                        trailingIcon = {
                            Checkbox(checked = viewState.autoApprove, onCheckedChange = null)
                        },
                        onClick = { viewModel.setAutoApprove(!viewState.autoApprove) }
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_usage)) },
                        onClick = {
                            showMenu = false
                            openInBrowser(context, Uri.parse(ChatGptAuthConfig.usageSettingsUrl))
                        }
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_different_account)) },
                        onClick = {
                            showMenu = false
                            viewModel.signIn({ uri -> openInBrowser(context, uri) }, newAccount = true)
                        }
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_sign_out)) },
                        onClick = {
                            showMenu = false
                            viewModel.signOut()
                        }
                    )
                }
            }
        }
        IconButton(onClick = onClose) {
            Icon(Icons.Default.Close, contentDescription = stringResource(Strings.reader_chat_close))
        }
    }
}

@Composable
private fun SignInPrompt(
    isSigningIn: Boolean,
    error: String?,
    onSignIn: ((Uri) -> Unit) -> Unit,
    onCancel: () -> Unit,
    onSignOut: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = stringResource(Strings.reader_chat_sign_in_title),
            style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(12.dp))
        Text(
            text = stringResource(Strings.reader_chat_sign_in_body),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        if (isSigningIn) {
            CircularProgressIndicator(modifier = Modifier.size(32.dp), strokeWidth = 2.dp)
            Spacer(Modifier.height(12.dp))
            Text(
                text = stringResource(Strings.reader_chat_signing_in),
                style = MaterialTheme.typography.bodyMedium,
            )
            TextButton(onClick = onCancel) {
                Text(stringResource(Strings.reader_chat_cancel))
            }
        } else {
            Button(onClick = { onSignIn { uri -> openInBrowser(context, uri) } }) {
                Text(stringResource(Strings.reader_chat_continue_with_chatgpt))
            }
            if (onSignOut != null) {
                TextButton(onClick = onSignOut) {
                    Text(stringResource(Strings.reader_chat_sign_out))
                }
            }
        }
        if (error != null) {
            Spacer(Modifier.height(16.dp))
            Text(
                text = error,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                textAlign = TextAlign.Center,
            )
        }
    }
}

@Composable
private fun ChatMessages(
    viewState: AiChatViewState,
    viewModel: AiChatViewModel,
    modifier: Modifier,
) {
    val listState = rememberLazyListState()
    val itemCount = viewState.turns.size + 1
    LaunchedEffect(viewState.turns.size, viewState.streamingText?.length, viewState.phase) {
        // A conversation restored after a fold or reopen arrives before the list has laid it out,
        // so wait until the list knows about every item before jumping to the end.
        snapshotFlow { listState.layoutInfo.totalItemsCount }.first { it >= itemCount }
        listState.scrollToItem(itemCount - 1)
        // Markdown measures asynchronously, so answers keep growing for a moment after they're laid
        // out; stay pinned to the end until they settle (or the user scrolls the list).
        withTimeoutOrNull(1_500) {
            snapshotFlow { listState.canScrollForward }.collect { canScrollForward ->
                if (canScrollForward && !listState.isScrollInProgress) {
                    listState.scrollBy(100_000f)
                }
            }
        }
    }
    if (viewState.turns.isEmpty() && viewState.phase == null && viewState.error == null) {
        Column(
            modifier = modifier.fillMaxWidth().padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = stringResource(
                    if (viewState.isLibrary) Strings.reader_chat_library_empty else Strings.reader_chat_empty
                ),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            if (viewState.isLibrary) {
                Spacer(Modifier.height(16.dp))
                librarySuggestions.forEach { suggestion ->
                    AssistChip(
                        onClick = { viewModel.runSuggestion(suggestion) },
                        label = { Text(suggestion) },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }
        return
    }
    val lastAnswerIndex = viewState.turns.indexOfLast { it.role == "assistant" }
    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
        contentPadding = PaddingValues(12.dp),
    ) {
        itemsIndexed(viewState.turns) { index, turn ->
            if (turn.role == "user") {
                UserMessage(turn)
            } else {
                ToolActivity(turn.activity)
                AssistantMessage(
                    text = turn.text,
                    actions = {
                        AnswerActions(
                            text = turn.text,
                            saved = index in viewState.savedAnswers,
                            canSave = viewState.canSave,
                            onSave = { viewModel.saveAnswerAsNote(index) },
                            onExplainAgain = if (!viewState.isLibrary && index == lastAnswerIndex && !viewState.isSending) {
                                viewModel::explainAgain
                            } else {
                                null
                            },
                        )
                    }
                )
            }
        }
        item {
            Column {
                if (viewState.isSending) ToolActivity(viewState.liveActivity)
                viewState.streamingText?.takeIf { it.isNotEmpty() }?.let { text ->
                    AssistantMessage(text = text, actions = null)
                }
                viewState.phase?.let { phase ->
                    StatusLine(phase)
                }
                viewState.error?.let { error ->
                    Text(
                        text = error,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                }
            }
        }
    }
}

/** What the assistant did in the library for an answer, one quiet line per tool. */
@Composable
private fun ToolActivity(lines: List<String>) {
    if (lines.isEmpty()) return
    Column(modifier = Modifier.padding(bottom = 4.dp)) {
        lines.forEach { line ->
            Text(
                text = "• $line",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@Composable
private fun StatusLine(phase: ChatPhase) {
    val label = when (phase) {
        ChatPhase.ReadingDocument -> stringResource(Strings.reader_chat_reading_document)
        ChatPhase.Thinking -> stringResource(Strings.reader_chat_thinking)
        ChatPhase.Writing -> stringResource(Strings.reader_chat_writing)
        is ChatPhase.RunningTool -> phase.label + "…"
    }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
        CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
        Spacer(Modifier.width(8.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun UserMessage(turn: ChatTurn) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            color = MaterialTheme.colorScheme.primaryContainer,
            modifier = Modifier.widthIn(max = 560.dp),
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
                if (turn.images.isNotEmpty()) {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        turn.images.forEach { ImageThumbnail(it, size = 72) }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                SelectionContainer {
                    Text(text = turn.text, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}

@Composable
private fun AssistantMessage(text: String, actions: (@Composable () -> Unit)?) {
    Column(modifier = Modifier.fillMaxWidth()) {
        // Finished answers with math are typeset; while streaming, the formulas aren't complete yet.
        if (actions != null && ChatMarkdown.containsMath(text)) {
            MathAnswer(text)
        } else {
            SelectionContainer {
                Markdown(
                    content = text,
                    typography = chatTypography(),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        }
        actions?.invoke()
    }
}

/** Markdown sized to sit with the rest of the panel rather than at document scale. */
@Composable
private fun chatTypography() = with(MaterialTheme.typography) {
    val body = bodyMedium
    val code = bodySmall.copy(fontFamily = FontFamily.Monospace)
    markdownTypography(
        h1 = titleLarge,
        h2 = titleMedium,
        h3 = titleSmall,
        h4 = titleSmall,
        h5 = titleSmall,
        h6 = titleSmall,
        text = body,
        code = code,
        inlineCode = code,
        quote = body.copy(fontStyle = FontStyle.Italic),
        paragraph = body,
        ordered = body,
        bullet = body,
        list = body,
    )
}

@Composable
private fun AnswerActions(
    text: String,
    saved: Boolean,
    canSave: Boolean,
    onSave: () -> Unit,
    onExplainAgain: (() -> Unit)?,
) {
    val context = LocalContext.current
    Row(verticalAlignment = Alignment.CenterVertically) {
        IconButton(onClick = { copyToClipboard(context, text) }, modifier = Modifier.size(36.dp)) {
            Icon(
                Icons.Default.ContentCopy,
                contentDescription = stringResource(Strings.reader_chat_copy),
                modifier = Modifier.size(18.dp),
            )
        }
        if (canSave) {
            TextButton(onClick = onSave, enabled = !saved) {
                Icon(
                    if (saved) Icons.Default.Check else Icons.Default.NoteAdd,
                    contentDescription = null,
                    modifier = Modifier.size(18.dp),
                )
                Spacer(Modifier.width(4.dp))
                Text(
                    stringResource(if (saved) Strings.reader_chat_saved_note else Strings.reader_chat_save_as_note),
                    style = MaterialTheme.typography.labelMedium,
                )
            }
        }
        if (onExplainAgain != null) {
            TextButton(onClick = onExplainAgain) {
                Icon(Icons.Default.Lightbulb, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(4.dp))
                Text(
                    stringResource(Strings.reader_chat_explain_again),
                    style = MaterialTheme.typography.labelMedium,
                )
            }
        }
    }
}

private fun copyToClipboard(context: Context, text: String) {
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText("AI answer", text))
}

@Composable
private fun ImageThumbnail(image: ChatImage, size: Int, onRemove: (() -> Unit)? = null) {
    val bitmap = remember(image.dataUrl) {
        runCatching {
            val bytes = Base64.decode(image.dataUrl.substringAfter(","), Base64.DEFAULT)
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
        }.getOrNull()
    }
    Box {
        if (bitmap != null) {
            Image(
                bitmap = bitmap,
                contentDescription = image.label,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .size(size.dp)
                    .clip(RoundedCornerShape(8.dp)),
            )
        }
        if (onRemove != null) {
            Surface(
                shape = RoundedCornerShape(50),
                color = MaterialTheme.colorScheme.inverseSurface,
                modifier = Modifier.align(Alignment.TopEnd).padding(2.dp).size(20.dp),
                onClick = onRemove,
            ) {
                Icon(
                    Icons.Default.Close,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.inverseOnSurface,
                    modifier = Modifier.padding(3.dp),
                )
            }
        }
    }
}

@Composable
private fun Composer(
    viewState: AiChatViewState,
    viewModel: AiChatViewModel,
    onAttachPage: (() -> Unit)?,
    onSelectArea: (() -> Unit)?,
) {
    var showAttachMenu by remember { mutableStateOf(false) }
    val imagesSupported = viewState.selectedModel?.supportsImages != false
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .navigationBarsPadding()
            .padding(horizontal = 8.dp, vertical = 6.dp)
    ) {
        if (!viewState.canSave) {
            Text(
                text = stringResource(Strings.reader_chat_not_saved),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, bottom = 4.dp),
            )
        }
        viewState.quote?.let { quote ->
            Surface(
                shape = RoundedCornerShape(10.dp),
                color = MaterialTheme.colorScheme.secondaryContainer,
                modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(modifier = Modifier.weight(1f).padding(start = 10.dp, top = 6.dp, bottom = 6.dp)) {
                        Text(
                            text = stringResource(Strings.reader_chat_quote_label),
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.SemiBold,
                        )
                        Text(
                            text = quote,
                            style = MaterialTheme.typography.bodySmall,
                            maxLines = 3,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    IconButton(onClick = { viewModel.setQuote(null) }) {
                        Icon(Icons.Default.Close, contentDescription = null)
                    }
                }
            }
            if (viewState.input.isBlank() && !viewState.isSending) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState())
                        .padding(bottom = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    ChatQuickAction.entries.forEach { action ->
                        AssistChip(
                            onClick = { viewModel.runQuickAction(action) },
                            label = { Text(action.label) },
                        )
                    }
                }
            }
        }
        if (viewState.images.isNotEmpty()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(bottom = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                viewState.images.forEach { image ->
                    ImageThumbnail(image, size = 56, onRemove = { viewModel.removeImage(image) })
                }
            }
        }
        Row(verticalAlignment = Alignment.Bottom) {
            if (onAttachPage != null && onSelectArea != null) Box {
                IconButton(onClick = { showAttachMenu = true }) {
                    Icon(Icons.Default.Add, contentDescription = stringResource(Strings.reader_chat_attach))
                }
                DropdownMenu(expanded = showAttachMenu, onDismissRequest = { showAttachMenu = false }) {
                    if (!imagesSupported) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Strings.reader_chat_images_unsupported)) },
                            onClick = {},
                            enabled = false,
                        )
                    }
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_attach_page)) },
                        leadingIcon = { Icon(Icons.Default.Description, contentDescription = null) },
                        enabled = imagesSupported,
                        onClick = {
                            showAttachMenu = false
                            onAttachPage()
                        }
                    )
                    DropdownMenuItem(
                        text = { Text(stringResource(Strings.reader_chat_attach_area)) },
                        leadingIcon = { Icon(Icons.Default.Crop, contentDescription = null) },
                        enabled = imagesSupported,
                        onClick = {
                            showAttachMenu = false
                            onSelectArea()
                        }
                    )
                }
            }
            OutlinedTextField(
                value = viewState.input,
                onValueChange = viewModel::setInput,
                modifier = Modifier.weight(1f).heightIn(min = 48.dp),
                placeholder = {
                    Text(
                        stringResource(
                            if (viewState.isLibrary) Strings.reader_chat_library_input_hint
                            else Strings.reader_chat_input_hint
                        )
                    )
                },
                maxLines = 6,
                shape = RoundedCornerShape(22.dp),
            )
            if (viewState.isSending) {
                IconButton(onClick = viewModel::stop) {
                    Icon(Icons.Default.Stop, contentDescription = stringResource(Strings.reader_chat_stop))
                }
            } else {
                IconButton(
                    onClick = { viewModel.send() },
                    enabled = viewState.input.isNotBlank(),
                ) {
                    Icon(
                        Icons.AutoMirrored.Filled.Send,
                        contentDescription = stringResource(Strings.reader_chat_send),
                    )
                }
            }
        }
        ModelPicker(viewState = viewState, viewModel = viewModel)
    }
}

@Composable
private fun ModelPicker(viewState: AiChatViewState, viewModel: AiChatViewModel) {
    var expanded by remember { mutableStateOf(false) }
    val model = viewState.selectedModel
    Box(modifier = Modifier.padding(start = if (viewState.isLibrary) 4.dp else 40.dp)) {
        TextButton(
            onClick = { expanded = true },
            enabled = viewState.models.isNotEmpty(),
            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp),
            modifier = Modifier.height(32.dp),
        ) {
            Text(
                text = listOfNotNull(
                    model?.displayName ?: stringResource(Strings.reader_chat_model),
                    viewState.reasoningEffort,
                ).joinToString(" · "),
                style = MaterialTheme.typography.labelMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = null, modifier = Modifier.size(16.dp))
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            viewState.models.forEach { option ->
                DropdownMenuItem(
                    text = { Text(option.displayName) },
                    leadingIcon = {
                        if (option.slug == model?.slug) Icon(Icons.Default.Check, contentDescription = null)
                        else Spacer(Modifier.size(24.dp))
                    },
                    onClick = { viewModel.selectModel(option) }
                )
            }
            val efforts = model?.reasoningEfforts.orEmpty()
            if (efforts.isNotEmpty()) {
                HorizontalDivider()
                Text(
                    text = stringResource(Strings.reader_chat_reasoning),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                )
                efforts.forEach { effort ->
                    DropdownMenuItem(
                        text = { Text(effort.replaceFirstChar { it.uppercase() }) },
                        leadingIcon = {
                            if (effort == viewState.reasoningEffort) Icon(Icons.Default.Check, contentDescription = null)
                            else Spacer(Modifier.size(24.dp))
                        },
                        onClick = {
                            viewModel.selectReasoningEffort(effort)
                            expanded = false
                        }
                    )
                }
            }
        }
    }
}
