package io.swarm.mobility

import android.content.Context
import java.util.UUID

/** A persisted request ID lets a lost response be retried without extending a lease. */
class CoordinationSession(context: Context) {
    private val prefs = context.getSharedPreferences("server_coordination_session", Context.MODE_PRIVATE)
    val pendingSpace: String? get() = prefs.getString("pending_space", null)
    val pendingRequest: String? get() = prefs.getString("pending_request", null)

    fun begin(space: String): String {
        pendingRequest?.let { return it }
        val request = UUID.randomUUID().toString()
        check(prefs.edit().putString("pending_space", space).putString("pending_request", request).commit())
        return request
    }

    fun clear() {
        check(prefs.edit().remove("pending_space").remove("pending_request").commit())
    }
}
