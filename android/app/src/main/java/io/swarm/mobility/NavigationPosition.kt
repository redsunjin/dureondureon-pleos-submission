package io.swarm.mobility

import kotlin.math.*

/** NaviHelper reports its own position without a source timestamp. Receipt age is not GPS age. */
data class NavigationPosition(val latitude: Double, val longitude: Double, val reportedElapsedMillis: Long) {
    fun valid() = latitude.isFinite() && longitude.isFinite() && latitude in -90.0..90.0 && longitude in -180.0..180.0
    fun recentlyReported(now: Long) = valid() && now >= reportedElapsedMillis && now - reportedElapsedMillis <= 10_000
}

enum class PositionAlignment { WAITING, STALE, DIFFERENT, MATCHED }

object NavigationPositionCheck {
    const val MAX_DISTANCE_METERS = 100.0 // Allows navigation road snapping, not a parking accuracy claim.
    fun distanceMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = sin(dLat / 2).pow(2) + cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) * sin(dLon / 2).pow(2)
        return 6_371_000.0 * 2 * asin(sqrt(a.coerceIn(0.0, 1.0)))
    }
    fun evaluate(fix: PositionFix?, navigation: NavigationPosition?, now: Long): PositionAlignment {
        if (fix == null || navigation == null || !fix.valid() || !navigation.valid()) return PositionAlignment.WAITING
        if (!fix.fresh(now) || !navigation.recentlyReported(now)) return PositionAlignment.STALE
        return if (distanceMeters(fix.latitude, fix.longitude, navigation.latitude, navigation.longitude) <= MAX_DISTANCE_METERS)
            PositionAlignment.MATCHED else PositionAlignment.DIFFERENT
    }
}
