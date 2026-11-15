package io.swarm.mobility

/** Location stays on the device; the parking API does not receive these coordinates. */
data class PositionFix(val latitude: Double, val longitude: Double, val confidence: Float,
    val sourceTimeMillis: Long, val receivedWallMillis: Long, val receivedElapsedMillis: Long) {
    fun valid(): Boolean = latitude.isFinite() && longitude.isFinite() &&
        latitude in -90.0..90.0 && longitude in -180.0..180.0 && confidence.isFinite() && sourceTimeMillis > 0
    fun fresh(nowElapsedMillis: Long): Boolean {
        val ageAtReceipt = receivedWallMillis - sourceTimeMillis
        if (!valid() || ageAtReceipt < -5_000 || nowElapsedMillis < receivedElapsedMillis) return false
        return ageAtReceipt.coerceAtLeast(0) + nowElapsedMillis - receivedElapsedMillis <= 15_000
    }
}

data class MobilityReading(val locationMessage: String, val position: PositionFix? = null,
    val needsLocationPermission: Boolean = false, val navigationMessage: String = "내비 연결 확인 중",
    val navigationReady: Boolean = false, val navigationBusy: Boolean = false,
    val navigationTermsRequired: Boolean = false,
    val routePresent: Boolean = false, val navigationPosition: NavigationPosition? = null)

interface MobilitySource {
    fun start(onReading: (MobilityReading) -> Unit)
    fun enableLocation()
    fun refresh()
    fun requestDemoRoute()
    fun cancelRoute()
    fun openNavigation()
    fun stop()
}

/** Official SDK example POI, unrelated to A-14/B-03/C-21 parking slots. */
object DemoDestination {
    const val LATITUDE = 37.48544722
    const val LONGITUDE = 127.03636666
    const val NAME = "연동 테스트 목적지"
    fun matches(latitude: Double, longitude: Double): Boolean =
        latitude.isFinite() && longitude.isFinite() &&
            kotlin.math.abs(latitude - LATITUDE) < 0.0001 && kotlin.math.abs(longitude - LONGITUDE) < 0.0001
}

/** Only service evidence can acknowledge a route or cancellation. */
class RouteProgress {
    var pending = false; private set
    var destinationMatched = false; private set
    var completed = false; private set
    var cancelled = false; private set
    private var activeRequest = false
    private var cancelling = false
    fun requested() { activeRequest = true; cancelling = false; pending = true; destinationMatched = false; completed = false; cancelled = false }
    fun destination(latitude: Double, longitude: Double) {
        destinationMatched = DemoDestination.matches(latitude, longitude)
    }
    fun routingCompleted() { if (activeRequest && !cancelling && !cancelled) { completed = true; if (destinationMatched) pending = false } }
    fun confirmDestination(latitude: Double, longitude: Double) {
        destination(latitude, longitude); if (destinationMatched && completed) pending = false
    }
    fun cancelRequested() { cancelling = true; completed = false; pending = true; cancelled = false }
    fun cancelAcknowledged() { activeRequest = false; cancelling = false; pending = false; destinationMatched = false; completed = false; cancelled = true }
    fun failed() { activeRequest = false; cancelling = false; pending = false; completed = false }
    val verified: Boolean get() = completed && destinationMatched && !cancelled
}
