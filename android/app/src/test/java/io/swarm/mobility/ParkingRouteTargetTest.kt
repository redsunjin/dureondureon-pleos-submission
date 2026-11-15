package io.swarm.mobility

import org.junit.Assert.*
import org.junit.Test

class ParkingRouteTargetTest {
    private val a=ParkingCandidate("A-14",60,"demo",false,"reserved",GeoPoint(37.48544722,127.03636666),"demo")
    private val b=ParkingCandidate("B-03",120,"demo",true,"available",GeoPoint(37.484291,127.033019),"demo")
    private val reservation=ServerReservation("lease-a","A-14","active",1_045_000,"request-a")
    private val state=CoordinationState(listOf(a,b),reservation,ReservationClock(45_000,1000))
    private fun target(value: CoordinationState?=state, connected: Boolean=true, now: Long=1000)=ParkingRouteTarget.resolve(value,connected,now)
    @Test fun destinationIsTheReservedSpaceRatherThanAnotherAvailableCandidate() {
        assertEquals("A-14",target()!!.spaceId);assertEquals(a.mapPosition,target()!!.point)
        assertNotEquals(b.mapPosition,target()!!.point)
    }
    @Test fun unconfirmedOrDisconnectedCannotRoute() {
        assertNull(target(null));assertNull(target(state.copy(reservation=null)));assertNull(target(connected=false))
    }
    @Test fun deadlineInvalidatesAtTheExactMillisecondWithoutWaitingForPoll() {
        assertNotNull(target(now=45_999));assertNull(target(now=46_000));assertNull(target(now=999))
    }
    @Test fun cancelArrivalAndExpiryInvalidateTheTarget() {
        for(status in listOf("cancelled","arrived","expired")) assertNull(target(state.copy(reservation=reservation.copy(status=status))))
    }
    @Test fun invalidMissingOrUnclassifiedCoordinatesCannotBecomeATarget() {
        for(space in listOf(a.copy(mapPosition=null),a.copy(mapPosition=GeoPoint(Double.NaN,127.0)),
            a.copy(coordinateSource=null),a.copy(coordinateSource="unknown"))) assertNull(target(state.copy(candidates=listOf(space))))
    }
    @Test fun missingDuplicatedOrInconsistentServerSpaceCannotRoute() {
        assertNull(target(state.copy(candidates=listOf(b))))
        assertNull(target(state.copy(candidates=listOf(a,a))))
        assertNull(target(state.copy(candidates=listOf(a.copy(available=true)))))
        assertNull(target(state.copy(candidates=listOf(a.copy(status="occupied")))))
    }
    @Test fun replacementLeaseOrUpdatedCoordinatesCannotAcceptThePreviousTarget() {
        val old=target()
        assertNotEquals(old,target(state.copy(reservation=reservation.copy(id="lease-new"))))
        assertNotEquals(old,target(state.copy(candidates=listOf(a.copy(mapPosition=b.mapPosition)))))
        assertNotEquals(old,target(state.copy(reservation=reservation.copy(spaceId="B-03"),
            candidates=listOf(a.copy(available=true,status="available"),b.copy(available=false,status="reserved")))))
    }
}
