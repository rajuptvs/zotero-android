package org.zotero.android.ai.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.zotero.android.ai.mcp.McpServer

/**
 * The user's own MCP servers, whose tools the chat can use (ai/spec/mcp-client.md). Kept on this
 * device only; tokens go to the encrypted store.
 */
@Composable
internal fun McpServersDialog(viewModel: AiChatViewModel, onDismiss: () -> Unit) {
    var servers by remember { mutableStateOf(viewModel.mcpServers()) }
    var name by remember { mutableStateOf("") }
    var url by remember { mutableStateOf("") }
    var token by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    fun refresh() { servers = viewModel.mcpServers() }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        title = { Text("MCP servers") },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    "The chat can use tools from your own MCP servers (https, or http on this device). " +
                        "Every call asks first unless you allow read-only tools for a server.",
                    style = MaterialTheme.typography.bodySmall,
                )
                if (servers.isEmpty()) Text("None yet.", style = MaterialTheme.typography.bodyMedium)
                servers.forEach { server ->
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(server.name, style = MaterialTheme.typography.titleSmall)
                                Text(server.url, style = MaterialTheme.typography.bodySmall)
                            }
                            Switch(checked = server.enabled, onCheckedChange = {
                                viewModel.saveMcpServer(server.copy(enabled = it), null, updateAuthorization = false)
                                refresh()
                            })
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(checked = server.autoReadOnly, onCheckedChange = {
                                viewModel.saveMcpServer(server.copy(autoReadOnly = it), null, updateAuthorization = false)
                                refresh()
                            })
                            Text("Run read-only tools without asking", style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                        }
                        Row {
                            TextButton(onClick = {
                                message = "Connecting to ${server.name}…"
                                scope.launch { message = viewModel.testMcpServer(server) }
                            }) { Text("Test") }
                            TextButton(onClick = {
                                viewModel.removeMcpServer(server.id)
                                refresh()
                            }) { Text("Remove") }
                        }
                        HorizontalDivider()
                    }
                }
                message?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary) }
                Text("Add a server", style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(top = 4.dp))
                OutlinedTextField(value = name, onValueChange = { name = it }, label = { Text("Name") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(
                    value = url, onValueChange = { url = it }, label = { Text("URL") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = token, onValueChange = { token = it }, label = { Text("Authorization header (optional)") },
                    placeholder = { Text("Bearer …") }, singleLine = true,
                    visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth(),
                )
                TextButton(onClick = {
                    message = runCatching {
                        viewModel.saveMcpServer(McpServer(id = "", name = name, url = url.trim()), token.ifBlank { null })
                        name = ""; url = ""; token = ""
                        refresh()
                        null
                    }.getOrElse { it.message }
                }, enabled = url.isNotBlank()) { Text("Add") }
            }
        },
    )
}
