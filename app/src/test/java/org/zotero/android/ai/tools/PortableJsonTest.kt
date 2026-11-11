package org.zotero.android.ai.tools

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Test

/** Export text is byte-identical to Python's json.dumps(ensure_ascii=False, separators=(",", ":")) and JS's JSON.stringify. */
class PortableJsonTest {
    @Test
    fun writesLikePythonAndJs() {
        // Python's and node's output for the same value (U+2028/U+2029 raw, controls escaped, DEL raw).
        val expected = "[{\"title\":\"a\u2028b\u2029c\",\"x\":\"\\\"\\\\\\n\\r\\t\\b\\f\\u0001\\u001f\u007F \u00E9 \uD83D\uDE00 </script>\",\"n\":[1,2.5,true,null],\"id\":\"K\"}]"
        assertEquals(expected, PortableJson.write(JsonParser.parseString(expected)))
    }
}
