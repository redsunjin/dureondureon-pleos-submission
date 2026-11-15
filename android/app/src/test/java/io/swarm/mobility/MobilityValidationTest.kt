package io.swarm.mobility

import org.junit.Assert.*
import org.junit.Test

class MobilityValidationTest {
    private fun fix(lat: Double = 37.5, lon: Double = 127.0, time: Long = 1_000_000) =
        PositionFix(lat, lon, 0.9f, time, 1_000_000, 10_000)

    @Test fun rejectsInvalidCoordinatesAndUnknownTimestamp() {
        assertFalse(fix(Double.NaN).valid())
        assertFalse(fix(91.0).valid())
        assertFalse(fix(lon = 181.0).valid())
        assertFalse(fix(time = 0).valid())
        assertTrue(fix(0.0, 0.0).valid())
    }
    @Test fun lastKnownPositionDoesNotBecomeFreshJustBecauseItWasReadAgain() {
        assertFalse(fix(time = 970_000).fresh(10_000))
        assertTrue(fix().fresh(25_000))
        assertFalse(fix().fresh(25_001))
    }
    @Test fun rejectsUnexpectedClockOffsets() {
        assertFalse(fix(time = 1_020_000).fresh(10_000))
        assertFalse(fix().fresh(9_999))
    }
    @Test fun sendingRequestAndSeeingADestinationAloneDoNotMeanRoutingCompleted() {
        val p = RouteProgress(); p.requested()
        assertFalse(p.verified)
        p.destination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        assertFalse(p.verified)
        p.routingCompleted(); assertTrue(p.verified)
    }
    @Test fun completionBeforeDestinationNeedsMatchingCoordinates() {
        val p = RouteProgress(); p.requested(); p.routingCompleted()
        p.confirmDestination(0.0, 0.0); assertFalse(p.verified)
        p.confirmDestination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        assertTrue(p.verified); assertFalse(p.pending)
    }
    @Test fun cancellationNeedsServiceAcknowledgement() {
        val p = RouteProgress(); p.requested(); p.cancelRequested()
        assertTrue(p.pending); assertFalse(p.cancelled)
        p.cancelAcknowledged(); assertTrue(p.cancelled); assertFalse(p.verified)
    }
    @Test fun lateCompletionCannotUndoCancellation() {
        val p = RouteProgress(); p.requested()
        p.confirmDestination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        p.cancelRequested(); p.routingCompleted()
        assertFalse(p.verified); assertTrue(p.pending)
        p.cancelAcknowledged(); p.routingCompleted()
        p.confirmDestination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        assertFalse(p.verified); assertFalse(p.pending)
    }
    @Test fun unsolicitedOrFailedCompletionCannotBecomeVerified() {
        val p = RouteProgress()
        p.confirmDestination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        p.routingCompleted(); assertFalse(p.verified)
        p.requested(); p.failed(); p.routingCompleted()
        p.confirmDestination(DemoDestination.LATITUDE, DemoDestination.LONGITUDE)
        assertFalse(p.verified)
    }

}
