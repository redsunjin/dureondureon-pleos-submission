package io.swarm.mobility

import org.junit.Assert.*
import org.junit.Test

class AppMapGeometryTest {
    private val origin=GeoPoint(37.48,127.03)
    private fun fix(lat: Double=origin.latitude,lon: Double=origin.longitude,time: Long=1_000_000)=PositionFix(lat,lon,5f,time,1_000_000,10_000)
    private fun route()=RoutePreview(origin,GeoPoint(37.485,127.036),origin,listOf(origin,GeoPoint(37.485,127.036)),1000.0,120.0)
    @Test fun mercatorHasCorrectCenterAndAxes() {
        val center=AppMapGeometry.world(GeoPoint(0.0,0.0),1)
        assertEquals(256.0,center.x,0.001);assertEquals(256.0,center.y,0.001)
        assertTrue(AppMapGeometry.world(GeoPoint(0.0,1.0),1).x>center.x)
        assertTrue(AppMapGeometry.world(GeoPoint(1.0,0.0),1).y<center.y)
    }
    @Test fun polesStayFiniteAndWithinTiles() {
        for (lat in listOf(-90.0,90.0)) {
            val p=AppMapGeometry.world(GeoPoint(lat,0.0),15)
            assertTrue(p.y.isFinite());assertTrue(p.y in 0.0..(256.0*(1 shl 15)))
        }
    }
    @Test fun datelineWrapsHorizontallyButNotOverThePoles() {
        assertEquals(MapTile(3,7,0),AppMapGeometry.tile(3,-1,0))
        assertEquals(MapTile(3,0,0),AppMapGeometry.tile(3,8,0))
        assertNull(AppMapGeometry.tile(3,0,-1));assertNull(AppMapGeometry.tile(3,0,8))
    }
    @Test fun routeUsesFreshGpsRatherThanNativeDefault() {
        assertEquals(origin,AppMapGeometry.origin(fix(),10_000))
        assertNotEquals(GeoPoint(37.4127212,127.094681),AppMapGeometry.origin(fix(),10_000))
    }
    @Test fun staleOrMissingGpsCannotCreateAnOrigin() {
        assertNull(AppMapGeometry.origin(null,10_000))
        assertNull(AppMapGeometry.origin(fix(time=900_000),10_000))
        assertNull(AppMapGeometry.origin(fix(),25_001))
    }
    @Test fun replyCannotAttachToAMovedGpsPosition() {
        assertTrue(AppMapGeometry.accepts(origin,fix(lat=37.4801),10_000))
        assertFalse(AppMapGeometry.accepts(origin,fix(lat=37.481),10_000))
        assertFalse(AppMapGeometry.accepts(origin,fix(),30_000))
    }
    @Test fun validRouteKeepsTheRequestedStartAndDestination() { assertTrue(route().valid()) }
    @Test fun rejectsWrongRoadSnapOrDestination() {
        assertFalse(route().copy(snappedOrigin=GeoPoint(37.0,127.0)).valid())
        assertFalse(route().copy(points=listOf(origin,GeoPoint(37.9,127.0))).valid())
    }
    @Test fun rejectsInvalidGeometryAndMetrics() {
        assertFalse(route().copy(points=listOf(origin,GeoPoint(Double.NaN,127.0))).valid())
        assertFalse(route().copy(points=listOf(origin)).valid())
        assertFalse(route().copy(distanceMeters=-1.0).valid())
        assertFalse(route().copy(durationSeconds=Double.NaN).valid())
    }
}
