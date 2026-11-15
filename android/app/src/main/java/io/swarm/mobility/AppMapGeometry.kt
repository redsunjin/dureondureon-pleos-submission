package io.swarm.mobility

import kotlin.math.*

data class GeoPoint(val latitude: Double, val longitude: Double) {
    fun valid() = latitude.isFinite() && longitude.isFinite() && latitude in -90.0..90.0 && longitude in -180.0..180.0
}
data class WorldPoint(val x: Double, val y: Double)
data class MapTile(val zoom: Int, val x: Int, val y: Int)
data class RoutePreview(val origin: GeoPoint, val destination: GeoPoint, val snappedOrigin: GeoPoint,
    val points: List<GeoPoint>, val distanceMeters: Double, val durationSeconds: Double) {
    fun valid() = origin.valid() && destination.valid() && snappedOrigin.valid() && points.size in 2..20_000 &&
        points.all { it.valid() } && distanceMeters.isFinite() && distanceMeters >= 0 &&
        durationSeconds.isFinite() && durationSeconds >= 0 &&
        AppMapGeometry.distance(origin, snappedOrigin) <= 300 &&
        AppMapGeometry.distance(points.first(), snappedOrigin) <= 50 &&
        AppMapGeometry.distance(points.last(), destination) <= 300
}

object AppMapGeometry {
    const val MAX_LATITUDE = 85.05112878
    fun world(point: GeoPoint, zoom: Int): WorldPoint {
        require(point.valid() && zoom in 0..20)
        val size = 256.0 * (1 shl zoom)
        val lat = Math.toRadians(point.latitude.coerceIn(-MAX_LATITUDE, MAX_LATITUDE))
        val x = ((point.longitude + 180.0) / 360.0 * size) % size
        return WorldPoint(x, ((1.0 - ln(tan(lat) + 1.0 / cos(lat)) / PI) / 2.0 * size).coerceIn(0.0, size))
    }
    fun tile(zoom: Int, x: Int, y: Int): MapTile? {
        if (zoom !in 0..20) return null
        val n = 1 shl zoom
        if (y !in 0 until n) return null
        return MapTile(zoom, ((x % n) + n) % n, y)
    }
    fun distance(a: GeoPoint, b: GeoPoint) = NavigationPositionCheck.distanceMeters(a.latitude,a.longitude,b.latitude,b.longitude)
    fun origin(fix: PositionFix?, now: Long): GeoPoint? = fix?.takeIf { it.fresh(now) }?.let { GeoPoint(it.latitude,it.longitude) }
    fun accepts(origin: GeoPoint, fix: PositionFix?, now: Long): Boolean = origin(fix, now)?.let { distance(origin,it) <= 50 } ?: false
}
