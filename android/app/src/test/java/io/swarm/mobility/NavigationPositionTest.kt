package io.swarm.mobility

import org.junit.Assert.*
import org.junit.Test

class NavigationPositionTest {
    private val fix = PositionFix(37.48, 127.03, 5f, 1_000_000, 1_000_000, 10_000)
    private fun nav(lat: Double = 37.48, lon: Double = 127.03, time: Long = 10_000) = NavigationPosition(lat, lon, time)
    @Test fun matchingLocationsAreConfirmed() {
        assertEquals(PositionAlignment.MATCHED, NavigationPositionCheck.evaluate(fix, nav(), 10_000))
        assertEquals(0.0, NavigationPositionCheck.distanceMeters(37.48,127.03,37.48,127.03), 0.001)
    }
    @Test fun defaultNavigationPositionCannotPassAsInjectedGps() {
        assertEquals(PositionAlignment.DIFFERENT, NavigationPositionCheck.evaluate(fix, nav(37.41,127.09),10_000))
    }
    @Test fun allowsRoadSnappingButRejectsLargeOffsets() {
        assertEquals(PositionAlignment.MATCHED,NavigationPositionCheck.evaluate(fix,nav(37.4807),10_000))
        assertEquals(PositionAlignment.DIFFERENT,NavigationPositionCheck.evaluate(fix,nav(37.4811),10_000))
    }
    @Test fun expiredNavigationReportNeedsAnewQuery() {
        assertEquals(PositionAlignment.STALE, NavigationPositionCheck.evaluate(fix,nav(),20_001))
        assertEquals(PositionAlignment.STALE, NavigationPositionCheck.evaluate(fix,nav(time=11_000),10_000))
    }
    @Test fun staleGpsCannotEnableRoutingEvenIfCoordinatesMatch() {
        assertEquals(PositionAlignment.STALE,NavigationPositionCheck.evaluate(fix.copy(sourceTimeMillis=900_000),nav(),10_000))
    }
    @Test fun invalidAndMissingLocationsDoNotVerify() {
        assertEquals(PositionAlignment.WAITING,NavigationPositionCheck.evaluate(fix,null,10_000))
        assertEquals(PositionAlignment.WAITING,NavigationPositionCheck.evaluate(null,nav(),10_000))
        assertEquals(PositionAlignment.WAITING,NavigationPositionCheck.evaluate(fix,nav(Double.NaN),10_000))
        assertFalse(nav(91.0).valid())
    }
    @Test fun geodesicHandlesDatelineAndOppositePoints() {
        assertTrue(NavigationPositionCheck.distanceMeters(0.0,179.999,0.0,-179.999) < 230)
        assertTrue(NavigationPositionCheck.distanceMeters(0.0,0.0,0.0,180.0).isFinite())
    }
}
