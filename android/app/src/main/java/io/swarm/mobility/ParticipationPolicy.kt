package io.swarm.mobility

/** Enables explicit responses only for a recently received, matching demo request. */
object ParticipationPolicy {
    fun token(input: String): String? {
        val value=input.trim().substringAfterLast('#')
        return value.takeIf { it.matches(Regex("[A-Za-z0-9_-]{43}")) }
    }
    fun allowed(action: String,status: String,decision: String,age: Long,pending: Boolean): Boolean {
        if(pending || age !in 0..2000)return false
        return when(action) {
            "approve","reject" -> status=="awaiting" && decision=="pending"
            "withdraw" -> status in setOf("awaiting","ready","running") && decision=="approved"
            else -> false
        }
    }
}
