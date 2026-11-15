package io.swarm.mobility

/** A display clock anchored to a server response, never an offline booking authority. */
data class ReservationClock(val remainingAtReceipt: Long, val receivedElapsedMillis: Long) {
    fun remainingMillis(nowElapsedMillis: Long): Long {
        if (nowElapsedMillis < receivedElapsedMillis) return 0
        return (remainingAtReceipt - (nowElapsedMillis - receivedElapsedMillis)).coerceAtLeast(0)
    }
}
