package org.zotero.android.ai.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.zotero.android.ai.semantic.SemanticModel

/**
 * Turns semantic search on for this device (ai/spec/semantic.md): turning it on downloads the
 * embedding model over Wi-Fi; turning it off deletes it.
 */
@Composable
internal fun SemanticSearchDialog(viewModel: AiChatViewModel, onDismiss: () -> Unit) {
    val state by viewModel.semanticSearchState.collectAsState(initial = null)
    val total = SemanticModel.megabytes(SemanticModel.TOTAL_SIZE)

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        title = { Text("Semantic search") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    "Lets the chat find papers by meaning, using the search index the Zotero desktop plugin builds. " +
                        "It needs a $total MB model on this device, downloaded over Wi-Fi.",
                    style = MaterialTheme.typography.bodySmall,
                )
                val current = state ?: return@Column
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Use semantic search", style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                    Switch(checked = current.enabled, onCheckedChange = viewModel::setSemanticSearch)
                }
                if (current.enabled) {
                    val done = SemanticModel.megabytes(current.downloaded)
                    when (current.model) {
                        "ready" -> Text("Model downloaded ($total MB on this device).", style = MaterialTheme.typography.bodySmall)
                        "downloading" -> {
                            LinearProgressIndicator(
                                progress = { (current.downloaded.toFloat() / current.total).coerceIn(0f, 1f) },
                                modifier = Modifier.fillMaxWidth(),
                            )
                            Text(
                                if (current.waiting) "Waiting for Wi-Fi and free storage… ($done of $total MB)"
                                else "Downloading the model… $done of $total MB",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                        "error" -> Text(
                            "Couldn't download the model: ${current.error}. Turn it off and on to try again.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                        else -> Text("Preparing the download…", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        },
    )
}
