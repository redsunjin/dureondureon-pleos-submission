package io.swarm.mobility

import org.junit.Assert.assertEquals
import org.junit.Test

class ReservationClockTest {
    @Test fun countsDownFromServerRemainingTime() {
        assertEquals(30_000L, ReservationClock(40_000, 10_000).remainingMillis(20_000))
    }
    @Test fun expiresAtExactBoundary() {
        assertEquals(1L, ReservationClock(45_000, 10_000).remainingMillis(54_999))
        assertEquals(0L, ReservationClock(45_000, 10_000).remainingMillis(55_000))
    }
    @Test fun doesNotReviveAfterExpiry() {
        assertEquals(0L, ReservationClock(45_000, 10_000).remainingMillis(100_000))
    }
    @Test fun backwardsMonotonicTimeFailsClosed() {
        assertEquals(0L, ReservationClock(45_000, 10_000).remainingMillis(9_999))
    }
    @Test fun aFreshResponseDoesNotImplyANewLease() {
        assertEquals(15_000L, ReservationClock(15_000, 40_000).remainingMillis(40_000))
    }
}
