package org.zotero.android.ai.auth

import android.app.Activity
import android.os.Bundle

/**
 * Target of the "Return to Zotero" link on the loopback callback page. It is launched into the
 * app's own task (singleTask) and finishes right away, which brings the reader back to the front.
 */
class ChatGptAuthReturnActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        finish()
    }
}
