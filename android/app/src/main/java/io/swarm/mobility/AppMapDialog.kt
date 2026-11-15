package io.swarm.mobility

import android.app.Activity
import android.app.Dialog
import android.graphics.Color
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.ViewGroup
import android.view.Window
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors

/** Online preview tied to a server-confirmed lease; never controls the vehicle. */
class AppMapDialog(private val activity: Activity, initial: MobilityReading,
    initialState: CoordinationState?, initialConnected: Boolean, initialBusy: Boolean,
    private val onReserve: (ParkingCandidate) -> Unit, private val onCancel: () -> Unit,
    private val onClosed: () -> Unit) {
    private val dialog=Dialog(activity)
    private val handler=Handler(Looper.getMainLooper())
    private val worker=Executors.newSingleThreadExecutor()
    private val client=PublicMapClient(File(activity.cacheDir,"visible-map-tiles"))
    private val positionText=TextView(activity)
    private val tileText=TextView(activity)
    private val routeText=TextView(activity)
    private val selectionText=TextView(activity)
    private val candidateRow=LinearLayout(activity)
    private val routeButton=Button(activity)
    private val reserveButton=Button(activity)
    private val cancelButton=Button(activity)
    private val map=GpsMapView(activity,client) { loaded,total,errors ->
        tileText.text=when { errors>0 -> "지도 일부 연결 실패 · 지도 재시도를 눌러 주세요. ($loaded/$total)"
            loaded<total -> "지도 불러오는 중 $loaded/$total"
            else -> "지도 배경 수신 완료 · ${loaded}개 타일" }
    }
    private var reading=initial
    private var state=initialState
    private var connected=initialConnected
    private var busy=initialBusy
    private var candidates=emptyList<ParkingCandidate>()
    private var selectedId: String?=null
    private var target: ParkingRouteTarget?=null
    private var preview: RoutePreview?=null
    private var attemptedOrigin: GeoPoint?=null
    private var pending=false
    private var autoRefresh=false
    private var targetNeedsRoute=false
    private var requestGeneration=0
    private var lastAttempt = -30_000L
    private var closed=false
    private fun text(value: String)=TextView(activity).apply { text=value;setTextColor(Color.rgb(224,235,241));textSize=14f }
    private fun button(label: String, action: () -> Unit)=Button(activity).apply {
        text=label;contentDescription=label;textSize=13f;setOnClickListener { action() }
    }
    init {
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        val layout=LinearLayout(activity).apply { orientation=LinearLayout.VERTICAL;setPadding(18,12,18,12);setBackgroundColor(Color.rgb(13,23,31)) }
        layout.addView(text("주차 후보 지도 · 차량 GPS 연동").apply { textSize=20f })
        layout.addView(text("시연용 주차 좌표 · 실제 시설 아님 · 경로 미리보기"))
        listOf(positionText,tileText,routeText).forEach { it.setTextColor(Color.WHITE);it.textSize=14f;layout.addView(it) }
        layout.addView(candidateRow)
        layout.addView(map,LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,0,1f))
        layout.addView(text("© OpenStreetMap contributors · 경로: OSRM · 점선: 도로와 공간의 연결").apply {
            setOnClickListener { runCatching { activity.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW,android.net.Uri.parse("https://www.openstreetmap.org/copyright"))) } }
        })
        selectionText.setTextColor(Color.WHITE);selectionText.textSize=14f;layout.addView(selectionText)
        val bookingRow=LinearLayout(activity)
        reserveButton.contentDescription="선택한 지도 후보 예약"
        reserveButton.setOnClickListener { candidates.singleOrNull { it.spaceId==selectedId }?.let(onReserve) }
        cancelButton.text="지도 예약 취소";cancelButton.contentDescription="지도 예약 취소"
        cancelButton.setOnClickListener { onCancel() }
        listOf(reserveButton,cancelButton).forEach { bookingRow.addView(it,LinearLayout.LayoutParams(0,ViewGroup.LayoutParams.WRAP_CONTENT,1f)) }
        layout.addView(bookingRow)
        routeButton.contentDescription="예약 공간까지 경로 보기";routeButton.setOnClickListener { requestRoute() }
        layout.addView(routeButton)
        val controls=LinearLayout(activity)
        listOf(button("축소") { map.zoomBy(-1) },button("확대") { map.zoomBy(1) },
            button("내 위치") { map.follow() },button("지도 재시도") { map.retry() },button("지도 닫기") { close() }).forEach {
            controls.addView(it,LinearLayout.LayoutParams(0,ViewGroup.LayoutParams.WRAP_CONTENT,1f))
        }
        layout.addView(controls)
        map.onCandidateSelected={ select(it) }
        dialog.setContentView(layout)
        routeText.text="후보를 선택해 예약하면 현재 GPS에서 예약 공간까지 경로를 연결합니다."
        dialog.setOnDismissListener { release() };dialog.show()
        val bounds=Rect()
        activity.window.decorView.getWindowVisibleDisplayFrame(bounds)
        dialog.window?.setLayout((bounds.width()-32).coerceAtLeast(300),(bounds.height()-48).coerceAtLeast(400))
        update(initial,initialState,initialConnected,initialBusy)
    }
    private fun select(id: String) { selectedId=id;update(reading,state,connected,busy) }
    fun update(value: MobilityReading, coordination: CoordinationState?, isConnected: Boolean, isBusy: Boolean) {
        if(closed)return
        reading=value;state=coordination;connected=isConnected;busy=isBusy
        val now=SystemClock.elapsedRealtime()
        val values=coordination?.candidates.orEmpty()
        if(values!=candidates) {
            candidates=values;candidateRow.removeAllViews()
            candidates.forEach { candidate ->
                candidateRow.addView(button("${candidate.spaceId} · ${status(candidate)}") { select(candidate.spaceId) }.apply {
                    contentDescription="${candidate.spaceId} 지도 후보"
                },LinearLayout.LayoutParams(0,ViewGroup.LayoutParams.WRAP_CONTENT,1f))
            }
        }
        val newTarget=ParkingRouteTarget.resolve(coordination,connected,now)
        if(newTarget!=target) {
            requestGeneration++;pending=false;preview=null;attemptedOrigin=null;map.route(null)
            target=newTarget;autoRefresh=newTarget!=null;targetNeedsRoute=newTarget!=null
            if(newTarget!=null)selectedId=newTarget.spaceId
            routeText.text=if(newTarget==null) "유효한 예약 경로 없음 · 예약 취소·만료 또는 관제 상태를 확인해 주세요."
                else "${newTarget.spaceId} 예약 확인 · GPS 경로 연결 대기"
            if(BuildConfig.DEBUG)Log.i("SwarmMap","parking target=${newTarget?.spaceId} reservation=${newTarget?.reservationId}")
        }
        if(selectedId !in candidates.map { it.spaceId })selectedId=candidates.firstOrNull()?.spaceId
        val origin=AppMapGeometry.origin(value.position,now)
        map.position(origin);map.candidates(candidates,selectedId,target?.spaceId)
        positionText.text=if(origin==null) "현재 위치 수신 대기 · 새 GPS가 필요합니다."
            else String.format(Locale.US,"현재 GPS %.6f, %.6f",origin.latitude,origin.longitude)
        val validPreview=preview?.let { AppMapGeometry.accepts(it.origin,value.position,now) } ?: true
        val validPending=attemptedOrigin?.let { AppMapGeometry.accepts(it,value.position,now) } ?: true
        if(!validPreview || (pending && !validPending)) {
            requestGeneration++;pending=false;preview=null;attemptedOrigin=null;map.route(null)
            routeText.text="위치가 바뀌었거나 오래됐습니다 · 새 GPS로 예약 경로 갱신 대기"
        }
        val selected=candidates.singleOrNull { it.spaceId==selectedId }
        val active=coordination?.reservation?.status=="active" && coordination.clock.remainingMillis(now)>0
        val remaining=coordination?.clock?.remainingMillis(now) ?: 0
        selectionText.text=selected?.let { "선택 ${it.spaceId} · ${status(it)} · ${if(it.coordinateSource=="demo") "시연 좌표" else if(it.coordinateSource=="facility") "시설 좌표" else "좌표 없음"}" }.orEmpty() +
            (if(active) " / 내 예약 ${coordination?.reservation?.spaceId} · ${(remaining+999)/1000}초" else " / 내 예약 없음") +
            (if(!connected) " · 관제 확인 대기" else "")
        reserveButton.text="${selectedId ?: "후보"} 예약하고 경로 보기"
        reserveButton.isEnabled=connected && !busy && !active && selected?.available==true && selected.mapPosition!=null
        cancelButton.isEnabled=connected && !busy && active
        routeButton.text=target?.let { "예약 ${it.spaceId}까지 경로 다시 보기" } ?: "예약 공간까지 경로 보기 · 예약 필요"
        routeButton.isEnabled=origin!=null && target!=null && !pending && now-lastAttempt>=2000
        if(autoRefresh && target!=null && origin!=null && !pending && preview==null &&
            now-lastAttempt >= if(targetNeedsRoute) 2000 else 30_000) requestRoute()
    }
    private fun status(candidate: ParkingCandidate)=when(candidate.status) { "available" -> "예약 가능"; "reserved" -> "예약 중"; else -> "주차 중" }
    private fun requestRoute() {
        if(closed || pending)return
        val now=SystemClock.elapsedRealtime()
        val destination=ParkingRouteTarget.resolve(state,connected,now) ?: return
        val origin=AppMapGeometry.origin(reading.position,now) ?: return
        if(now-lastAttempt<2000)return
        lastAttempt=now;pending=true;autoRefresh=true;targetNeedsRoute=false;attemptedOrigin=origin
        val token=++requestGeneration
        preview=null;map.route(null);map.retry();routeButton.isEnabled=false
        routeText.text="예약 ${destination.spaceId} · 현재 GPS에서 경로 계산 중 (출발·목적지 OSRM 전송)"
        if(BuildConfig.DEBUG)Log.i("SwarmMap","route requested space=${destination.spaceId} reservation=${destination.reservationId} origin=${origin.latitude},${origin.longitude} destination=${destination.point}")
        worker.execute {
            val result=runCatching { client.route(origin,destination.point) }
            handler.post {
                if(closed || token!=requestGeneration)return@post
                pending=false
                val received=SystemClock.elapsedRealtime()
                if(destination!=ParkingRouteTarget.resolve(state,connected,received) || !AppMapGeometry.accepts(origin,reading.position,received)) {
                    preview=null;map.route(null);routeText.text="예약 또는 위치가 바뀌었습니다 · 이전 경로를 표시하지 않습니다.";return@post
                }
                result.onSuccess {
                    preview=it;map.route(it)
                    routeText.text=String.format(Locale.US,"예약 %s 경로 %.0fm · 약 %.0f분 · 도로→공간 %.0fm · 실제 주행 안내 아님",destination.spaceId,it.distanceMeters,it.durationSeconds/60,AppMapGeometry.distance(it.points.last(),destination.point))
                    if(BuildConfig.DEBUG)Log.i("SwarmMap","route shown space=${destination.spaceId} reservation=${destination.reservationId} destination=${it.destination} points=${it.points.size}")
                }.onFailure {
                    autoRefresh=false
                    routeText.text="예약 ${destination.spaceId} 경로 조회 실패 · 연결 상태와 도로 위치를 확인하고 다시 눌러 주세요."
                    Log.w("SwarmMap","route unavailable",it)
                }
            }
        }
    }
    fun close() { if(dialog.isShowing)dialog.dismiss() else release() }
    private fun release() {
        if(closed)return
        closed=true;requestGeneration++;autoRefresh=false
        client.close();map.close();worker.shutdownNow();handler.removeCallbacksAndMessages(null)
        Log.i("SwarmMap","map closed; requests cancelled");onClosed()
    }
}
