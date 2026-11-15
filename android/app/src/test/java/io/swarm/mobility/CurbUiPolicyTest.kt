package io.swarm.mobility

import org.junit.Assert.*
import org.junit.Test

class CurbUiPolicyTest {
    private val id="plan-1"
    private val car=CurbVehicle("P-1","뒤 차량",4.6,1.8,CurbPose(3.8,1.5,0.0),"${id}_P-1","pending","waiting",0.0,2.19,listOf(CurbStep("create_space",-1.06)))
    private val session=CurbSession(id,1,"awaiting","awaiting_consent",4,30.0,7.5,5.4,7.89,0.0,81.43,"waiting",null,listOf(car),ReservationClock(180_000,1000))
    private fun allow(action: String,s: CurbSession?=session,connected: Boolean=true,busy: Boolean=false,now: Long=1000)=
        CurbUiPolicy.allowed(CurbSnapshot(s,1000),connected,busy,now,action,"P-1")
    @Test fun no_plan_allows_create_only_after_authoritative_contact() {
        assertTrue(allow("create",s=null));assertFalse(allow("approve",s=null));assertFalse(allow("create",s=null,connected=false))
    }
    @Test fun awaiting_pending_vehicle_can_approve_or_reject_but_cannot_start() {
        assertTrue(allow("approve"));assertTrue(allow("reject"));assertFalse(allow("start"));assertFalse(allow("withdraw"));assertFalse(allow("create"))
    }
    @Test fun ready_requires_every_vehicle_approval_before_start() {
        val ready=session.copy(status="ready",vehicles=listOf(car.copy(decision="approved")))
        assertTrue(allow("start",ready));assertTrue(allow("withdraw",ready))
        assertFalse(allow("start",ready.copy(vehicles=listOf(car))))
        assertFalse(allow("approve",ready))
    }
    @Test fun busy_or_pending_request_cannot_send_another_action() {
        assertFalse(allow("approve",busy=true));assertFalse(allow("stop",busy=true))
    }
    @Test fun lost_or_stale_response_cannot_authorize_actions_or_progress() {
        assertFalse(allow("approve",connected=false));assertTrue(allow("approve",now=3000));assertFalse(allow("approve",now=3001));assertFalse(allow("approve",now=999))
    }
    @Test fun local_plan_deadline_disables_decisions_without_extending_server_lease() {
        assertFalse(allow("approve",session.copy(clock=ReservationClock(100,1000)),now=1100))
    }
    @Test fun terminal_session_can_only_create_a_new_plan() {
        for(status in listOf("rejected","stopped","completed","expired")) {
            val s=session.copy(status=status)
            assertTrue(allow("create",s));assertFalse(allow("start",s));assertFalse(allow("approve",s));assertFalse(allow("withdraw",s));assertFalse(allow("stop",s))
        }
    }
    @Test fun running_approved_participant_can_withdraw_without_approving_again() {
        val s=session.copy(status="running",actor="P-1",vehicles=listOf(car.copy(decision="approved",progress=12.0,motion="moving")))
        assertTrue(allow("withdraw",s));assertTrue(allow("stop",s));assertFalse(allow("approve",s));assertFalse(allow("start",s))
    }
    @Test fun completion_gap_text_uses_actual_server_poses_only_after_completion() {
        val rear=car.copy(pose=CurbPose(8.88,1.5,0.0))
        val front=car.copy(id="P-2",moveRequestId="${id}_P-2",pose=CurbPose(19.12,1.5,0.0))
        val own=car.copy(id="NEW",length=4.8,moveRequestId="${id}_NEW",pose=CurbPose(14.0,1.5,0.0))
        val s=session.copy(status="completed",vehicles=listOf(rear,front,own))
        assertEquals(0.42,s.completedGaps()!!.first,0.00001);assertEquals(0.42,s.completedGaps()!!.second,0.00001)
        assertNull(s.copy(status="running").completedGaps())
    }
    @Test fun current_arrangement_replan_only_allowed_for_interrupted_terminal_sessions() {
        for(status in listOf("stopped","rejected","expired")) assertTrue(allow("replan",session.copy(status=status)))
        for(status in listOf("awaiting","ready","running","completed")) assertFalse(allow("replan",session.copy(status=status)))
        assertFalse(allow("replan",s=null))
    }
    @Test fun replan_requires_fresh_contact_and_does_not_reuse_expired_lease() {
        val old=session.copy(status="expired",clock=ReservationClock(0,1000))
        assertTrue(allow("replan",old));assertFalse(allow("replan",old,connected=false))
        assertFalse(allow("replan",old,busy=true));assertFalse(allow("replan",old,now=3001))
    }
    @Test fun replan_metadata_requires_distinct_parent_and_valid_checkpoint_link() {
        val s=session.copy(replanGeneration=1,parentSessionId="parent",parentRevision=9,replanMode="remaining_path_revalidated",checkpointHash="a".repeat(64))
        assertTrue(s.valid());assertFalse(s.copy(parentSessionId=s.id).valid());assertFalse(s.copy(checkpointHash="bad").valid());assertFalse(s.copy(replanMode="unknown").valid())
    }
    @Test fun stationary_vehicle_must_still_approve_a_hold_request() {
        val s=session.copy(vehicles=listOf(car.copy(totalTravel=0.0,steps=emptyList())))
        assertTrue(s.valid());assertTrue(allow("approve",s));assertFalse(allow("start",s.copy(status="ready")))
        assertTrue(s.copy(vehicles=listOf(s.vehicles[0].copy(motion="holding"))).valid())
    }
    @Test fun four_new_approvals_cannot_reuse_the_fifth_old_vote() {
        val cars=(1..5).map { i->car.copy(id="P-$i",moveRequestId="${id}_P-$i",decision=if(i<5)"approved" else "pending") }
        assertFalse(allow("start",session.copy(status="ready",vehicles=cars)))
        assertTrue(allow("start",session.copy(status="ready",vehicles=cars.map { it.copy(decision="approved") })))
    }
    @Test fun invalid_poses_progress_requests_or_duplicate_ids_are_rejected() {
        for(c in listOf(car.copy(pose=CurbPose(Double.NaN,1.5,0.0)),car.copy(progress=101.0),car.copy(moveRequestId="wrong"),car.copy(totalTravel=-1.0))) {
            assertFalse(session.copy(vehicles=listOf(c)).valid());assertFalse(allow("approve",session.copy(vehicles=listOf(c))))
        }
        assertFalse(session.copy(vehicles=listOf(car,car)).valid());assertFalse(session.copy(actor="missing").valid())
    }
    @Test fun changed_scene_can_be_reported_during_motion_or_reconsent_but_not_after_completion() {
        for(status in listOf("awaiting","ready","running","stopped","rejected","expired"))assertTrue(allow("observe",session.copy(status=status)))
        assertFalse(allow("observe",session.copy(status="completed")));assertFalse(allow("observe",s=null))
        assertFalse(allow("observe",connected=false));assertFalse(allow("observe",busy=true));assertFalse(allow("observe",now=3001))
    }
    @Test fun changed_environment_search_is_valid_linked_metadata() {
        val s=session.copy(replanGeneration=1,parentSessionId="old",parentRevision=8,replanMode="changed_environment_search",checkpointHash="b".repeat(64),environmentRevision=1)
        assertTrue(s.valid());assertFalse(s.copy(environmentRevision=-1).valid())
    }
    @Test fun obstacles_and_route_geometry_reject_nonfinite_or_duplicate_inputs() {
        val obstacle=CurbObstacle("BOX",CurbPose(16.0,5.9,0.0),.25,.25)
        val s=session.copy(obstacles=listOf(obstacle),plannedPath=listOf(CurbPose(5.0,4.55,0.0),CurbPose(14.0,1.5,0.0)))
        assertTrue(s.valid());assertFalse(s.copy(obstacles=listOf(obstacle,obstacle)).valid())
        assertFalse(s.copy(obstacles=listOf(obstacle.copy(length=-1.0))).valid())
        assertFalse(s.copy(obstacles=listOf(obstacle.copy(id="P-1"))).valid())
        assertFalse(s.copy(plannedPath=listOf(CurbPose(Double.NaN,1.5,0.0))).valid())
    }
    @Test fun observed_stop_never_allows_previous_approvals_to_start() {
        val changed=session.copy(status="stopped",reason="environment_changed",environmentRevision=1)
        assertFalse(allow("start",changed));assertFalse(allow("approve",changed));assertTrue(allow("replan",changed))
        assertTrue(CurbLabels.reason(changed.reason).contains("재계획"))
        assertTrue(CurbLabels.stage("replan_move").contains("변경 배치"))
    }
    @Test fun moving_risk_stale_or_clearance_dwell_cannot_authorize_motion() {
        for(safety in listOf("risk","stale","clearing")) {
            val t=CurbTraffic(safety=safety,source="observed",safeToReplan=false,clearRemainingMillis=1000)
            assertTrue(t.valid())
            assertFalse(allow("replan",session.copy(status="stopped",traffic=t)))
            assertFalse(allow("approve",session.copy(traffic=t)))
            assertFalse(allow("start",session.copy(status="ready",traffic=t,vehicles=listOf(car.copy(decision="approved")))))
            assertTrue(allow("stop",session.copy(traffic=t)))
            assertTrue(allow("traffic",session.copy(status="stopped",traffic=t)))
        }
    }
    @Test fun clear_forecast_never_replaces_new_votes_or_explicit_start() {
        val t=CurbTraffic(safety="clear",source="scripted")
        val s=session.copy(status="stopped",reason="traffic_cleared",traffic=t)
        assertTrue(allow("replan",s));assertFalse(allow("start",s))
        assertFalse(allow("start",s.copy(status="ready")))
        assertTrue(allow("start",s.copy(status="ready",vehicles=listOf(car.copy(decision="approved")))))
    }
    @Test fun elapsed_local_clearance_countdown_cannot_enable_a_server_hold() {
        val t=CurbTraffic(safety="clearing",source="scripted",safeToReplan=false,clearRemainingMillis=0)
        assertTrue(t.valid());assertFalse(allow("replan",session.copy(status="stopped",traffic=t)))
        assertFalse(t.copy(safeToReplan=true).valid())
    }
    @Test fun moving_geometry_source_speed_and_prediction_must_be_valid() {
        val o=CurbMovingObstacle("PED","pedestrian",CurbPose(14.0,5.0,0.0),CurbPose(14.0,-2.0,0.0),.6,.6,0.0,-.6)
        val t=CurbTraffic(safety="risk",source="scripted",tracks=listOf(o),safeToReplan=false,riskId="PED",timeToRisk=4.0)
        assertTrue(t.valid());assertFalse(t.copy(tracks=listOf(o,o)).valid())
        assertFalse(t.copy(tracks=listOf(o.copy(vx=11.0))).valid())
        assertFalse(t.copy(tracks=listOf(o.copy(predictedPose=CurbPose(Double.NaN,1.0,0.0)))).valid())
        assertFalse(t.copy(source="unknown").valid());assertFalse(t.copy(timeToRisk=-1.0).valid())
    }
    @Test fun lost_connection_still_blocks_a_clear_forecast() {
        val s=session.copy(status="stopped",traffic=CurbTraffic(safety="clear",source="observed"))
        assertFalse(allow("replan",s,connected=false));assertFalse(allow("replan",s,now=3001))
        assertFalse(allow("traffic",s,busy=true))
    }
    @Test fun traffic_reasons_distinguish_predicted_risk_stale_observation_and_clearance() {
        assertTrue(CurbLabels.reason("traffic_risk").contains("위험"))
        assertTrue(CurbLabels.reason("traffic_stale").contains("새 관측"))
        assertTrue(CurbLabels.reason("traffic_cleared").contains("재동의"))
    }
}
