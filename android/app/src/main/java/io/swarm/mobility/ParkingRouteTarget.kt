package io.swarm.mobility

/** Selection is visual only. The route destination always comes from the live own reservation. */
data class ParkingRouteTarget(val reservationId: String, val spaceId: String, val point: GeoPoint,
    val coordinateSource: String) {
    companion object {
        fun resolve(state: CoordinationState?, connected: Boolean, now: Long): ParkingRouteTarget? {
            if (!connected || state == null || state.clock.remainingMillis(now) <= 0) return null
            val reservation = state.reservation?.takeIf { it.status == "active" } ?: return null
            val space = state.candidates.singleOrNull { it.spaceId == reservation.spaceId } ?: return null
            if (space.available || space.status != "reserved") return null
            val point = space.mapPosition?.takeIf { it.valid() } ?: return null
            val source = space.coordinateSource?.takeIf { it in setOf("demo", "facility") } ?: return null
            return ParkingRouteTarget(reservation.id, space.spaceId, point, source)
        }
    }
}
