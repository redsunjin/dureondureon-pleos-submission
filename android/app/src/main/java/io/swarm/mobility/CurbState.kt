package io.swarm.mobility

import kotlin.math.abs

data class CurbPose(val x: Double, val y: Double, val yaw: Double) {
    fun valid()=x.isFinite() && y.isFinite() && yaw.isFinite() && abs(yaw)<=Math.PI
}
data class CurbStep(val stage: String,val distance: Double)
data class CurbObstacle(val id: String,val pose: CurbPose,val length: Double,val width: Double)
data class CurbMovingObstacle(val id: String,val kind: String,val pose: CurbPose,val predictedPose: CurbPose,
    val length: Double,val width: Double,val vx: Double,val vy: Double)
data class CurbTraffic(val safety: String="disabled",val source: String?=null,val tracks: List<CurbMovingObstacle> = emptyList(),
    val safeToReplan: Boolean=true,val horizon: Double=12.0,val clearRemainingMillis: Long=0,
    val riskId: String?=null,val timeToRisk: Double?=null) {
    fun valid()=safety in setOf("disabled","risk","stale","clearing","clear") &&
        safeToReplan==(safety in setOf("disabled","clear")) && horizon.isFinite() && horizon in 0.1..30.0 &&
        clearRemainingMillis in 0..1000 && (source==null || source in setOf("scripted","observed")) &&
        (timeToRisk==null || timeToRisk.isFinite() && timeToRisk>=0) && tracks.size<=8 && tracks.map { it.id }.distinct().size==tracks.size && tracks.all {
            it.id.matches(Regex("[A-Za-z0-9_-]{1,60}")) && it.kind in setOf("pedestrian","vehicle") && it.pose.valid() && it.predictedPose.valid() &&
                it.length.isFinite() && it.length in 0.00001..8.0 && it.width.isFinite() && it.width in 0.00001..3.0 &&
                it.vx.isFinite() && it.vy.isFinite() && Math.hypot(it.vx,it.vy)<=10.0
        }
}
data class CurbVehicle(val id: String,val role: String,val length: Double,val width: Double,
    val pose: CurbPose,val moveRequestId: String,val decision: String,val motion: String,
    val progress: Double,val totalTravel: Double,val steps: List<CurbStep>)
data class CurbSession(val id: String,val revision: Int,val status: String,val reason: String,
    val rate: Int,val roadLength: Double,val roadWidth: Double,val initialGap: Double,val openingGap: Double,
    val elapsed: Double,val duration: Double,val stage: String,val actor: String?,val vehicles: List<CurbVehicle>,
    val clock: ReservationClock,val replanGeneration: Int=0,val parentSessionId: String?=null,
    val parentRevision: Int?=null,val replanMode: String?=null,val checkpointHash: String?=null,
    val environmentRevision: Int=0,val obstacles: List<CurbObstacle> = emptyList(),val plannedPath: List<CurbPose> = emptyList(),val traffic: CurbTraffic=CurbTraffic()) {
    fun active()=status in setOf("awaiting","ready","running")
    fun completedGaps(): Pair<Double,Double>? {
        if(status!="completed")return null
        val own=vehicles.find { it.id=="NEW" } ?: return null
        val rear=vehicles.filter { it.id!="NEW" && it.pose.x<own.pose.x }.maxByOrNull { it.pose.x } ?: return null
        val front=vehicles.filter { it.id!="NEW" && it.pose.x>own.pose.x }.minByOrNull { it.pose.x } ?: return null
        return (own.pose.x-own.length/2-rear.pose.x-rear.length/2) to (front.pose.x-front.length/2-own.pose.x-own.length/2)
    }
    fun replanMetadataValid()=replanGeneration>=0 && (replanGeneration==0 ||
        (parentSessionId?.matches(Regex("[A-Za-z0-9_-]{1,80}"))==true && parentRevision!=null && parentRevision>0 &&
         parentSessionId!=id && replanMode in setOf("current_pose_search","remaining_path_revalidated","changed_environment_search") &&
         checkpointHash?.matches(Regex("[a-f0-9]{64}"))==true))
    fun valid(): Boolean = replanMetadataValid() && traffic.valid() && environmentRevision>=0 && obstacles.size<=12 && obstacles.map { it.id }.distinct().size==obstacles.size &&
        obstacles.all { it.pose.valid() && it.length.isFinite() && it.width.isFinite() && it.length>0 && it.width>0 && vehicles.none { v->v.id==it.id } } &&
        plannedPath.size<=200 && plannedPath.all { it.valid() } && id.matches(Regex("[A-Za-z0-9_-]{1,80}")) && revision>0 && rate in 1..4 &&
        status in setOf("awaiting","ready","running","completed","rejected","stopped","expired") &&
        listOf(roadLength,roadWidth,initialGap,openingGap,elapsed,duration).all { it.isFinite() && it>=0 } &&
        roadLength>0 && roadWidth>0 && duration>0 && elapsed<=duration+0.001 &&
        vehicles.size in 1..64 && (actor==null || vehicles.any { it.id==actor }) && vehicles.map { it.id }.distinct().size==vehicles.size && vehicles.all {
            it.id.matches(Regex("[A-Za-z0-9_-]{1,80}")) && it.moveRequestId=="${id}_${it.id}" &&
            it.length.isFinite() && it.length>0 && it.width.isFinite() && it.width>0 && it.pose.valid() &&
            it.pose.x in 0.0..roadLength && it.pose.y in 0.0..roadWidth &&
            it.decision in setOf("pending","approved","rejected","withdrawn") &&
            it.motion in setOf("waiting","moving","steering","complete","stopped","holding") &&
            it.progress.isFinite() && it.progress in 0.0..100.0 && it.totalTravel.isFinite() && it.totalTravel>=0 &&
            it.steps.all { s->s.distance.isFinite() }
        }
}
data class CurbSnapshot(val session: CurbSession?,val receivedElapsed: Long)

object CurbUiPolicy {
    const val FRESH_MILLIS=2000L
    fun fresh(snapshot: CurbSnapshot?, connected: Boolean, now: Long): Boolean =
        connected && snapshot!=null && now>=snapshot.receivedElapsed && now-snapshot.receivedElapsed<=FRESH_MILLIS && (snapshot.session==null || snapshot.session.valid())
    fun allowed(snapshot: CurbSnapshot?, connected: Boolean, busy: Boolean, now: Long,
        action: String, vehicleId: String?=null): Boolean {
        if(busy || !fresh(snapshot,connected,now))return false
        val s=snapshot?.session
        if(action=="create")return s==null || !s.active()
        if(action=="replan")return s!=null && s.traffic.safeToReplan && s.status in setOf("stopped","rejected","expired")
        if(action in setOf("observe","traffic"))return s!=null && s.status!="completed"
        if(s==null || !s.valid() || !s.active() || s.clock.remainingMillis(now)<=0)return false
        if(action=="start")return s.traffic.safeToReplan && s.status=="ready" && s.vehicles.all { it.decision=="approved" }
        if(action=="stop")return true
        val car=s.vehicles.singleOrNull { it.id==vehicleId } ?: return false
        return when(action) {
            "approve" -> s.traffic.safeToReplan && s.status=="awaiting" && car.decision=="pending"
            "reject" -> s.status=="awaiting" && car.decision=="pending"
            "withdraw" -> car.decision=="approved"
            else -> false
        }
    }
}

object CurbLabels {
    fun stage(value: String)=when(value) {
        "waiting" -> "이동 시작 전"
        "create_space" -> "주변 차량 간격 조정"
        "approach" -> "새 차량 접근"
        "steering_at_rest" -> "정지 후 조향 방향 변경"
        "reverse_in" -> "후진 진입"
        "reverse_align" -> "후진 정렬"
        "final_position" -> "중앙 정차"
        "settle_spacing" -> "주차 후 간격 재정렬"
        "replan_move" -> "변경 배치 경로 이동"
        "parked" -> "평행주차 완료"
        "stopped" -> "이동 중단"
        else -> "진행 상태 확인"
    }
    fun reason(value: String)=when(value) {
        "awaiting_reconsent" -> "현재 배치 재계획 · 차량별 재동의 대기 · 이동하지 않습니다."
        "awaiting_consent" -> "차량별 동의 대기 · 아직 이동하지 않습니다."
        "all_approved" -> "전 차량 동의 완료 · 시작 버튼을 눌러 주세요."
        "running" -> "승인된 이동 계획 진행 중"
        "completed" -> "평행주차·간격 재정렬 완료"
        "vehicle_rejected" -> "차량이 요청을 거부해 계획을 시작하지 않습니다."
        "consent_withdrawn" -> "동의 철회로 모든 이동을 중단했습니다."
        "connection_lost" -> "연결 공백으로 중단 · 마지막 수신 위치 유지"
        "server_restarted" -> "서버 재시작으로 중단 · 같은 계획을 자동 재개하지 않습니다."
        "plan_expired" -> "계획 유효 시간 만료 · 새 계획이 필요합니다."
        "user_stopped" -> "사용자 요청으로 중단했습니다."
        "environment_changed" -> "장애물·차량 위치 변경으로 중단 · 바뀐 배치에서 재계획이 필요합니다."
        "traffic_risk" -> "이동 장애물 충돌 위험 · 현재 배치에서 중단 · 안전 확인 대기"
        "traffic_stale" -> "이동 장애물 관측이 오래됨 · 중단 유지 · 새 관측 필요"
        "traffic_cleared" -> "위험 해소 확인 · 현재 배치에서 재계획·전 차량 재동의 필요"
        else -> "관제 상태 확인 필요"
    }
}
