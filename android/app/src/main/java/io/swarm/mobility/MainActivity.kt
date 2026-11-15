package io.swarm.mobility

import android.app.Activity
import android.os.Bundle
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import java.util.concurrent.Executors
import android.view.WindowInsets
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class MainActivity : Activity() {
    private val vehicleId = "V-07" // Synthetic coordination ID; never upload a real VIN.
    private lateinit var vehicleSource: VehicleStateSource
    private lateinit var vehicleStatus: TextView
    private lateinit var mobilitySource: MobilitySource
    private var mobilityReading = MobilityReading("위치 확인 중")
    private lateinit var locationStatus: TextView
    private lateinit var navigationStatus: TextView
    private lateinit var positionAlignmentStatus: TextView
    private lateinit var locationPermissionButton: Button
    private lateinit var routeButton: Button
    private lateinit var cancelRouteButton: Button
    private lateinit var openNavigationButton: Button
    private lateinit var appMapButton: Button
    private var appMapDialog: AppMapDialog? = null
    private lateinit var client: CoordinationClient
    private lateinit var session: CoordinationSession
    private var state: CoordinationState? = null
    private var connected = false
    private var busy = false
    private var generation = 0
    private var visible = false
    private var errorMessage: String? = null
    private val worker = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var candidateContainer: LinearLayout
    private lateinit var eventLog: TextView
    private lateinit var connectionLabel: TextView
    private lateinit var refreshButton: Button
    private lateinit var cancelButton: Button
    private lateinit var arrivalButton: Button
    private val reservationButtons = mutableListOf<Pair<Button, ParkingCandidate>>()
    private val ticker = object : Runnable {
        override fun run() {
            updateStatus()
            if (visible) handler.postDelayed(this, 250)
        }
    }
    private val poller = object : Runnable {
        override fun run() {
            if (!busy && visible) synchronize()
            if (visible) handler.postDelayed(this, 3000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        session = CoordinationSession(this)
        client = CoordinationClient(BuildConfig.COORDINATION_BASE_URL, vehicleId)
        vehicleSource = VehicleSourceFactory.create(this)
        mobilitySource = MobilitySourceFactory.create(this)
        renderInitialScreen()
    }

    override fun onStart() {
        super.onStart()
        visible = true
        generation++
        busy = false
        connected = false
        ticker.run()
        poller.run()
        val viewGeneration = generation
        vehicleSource.start { reading ->
            handler.post {
                if (visible && viewGeneration == generation) {
                    vehicleStatus.text = "${reading.source}\n${reading.message}\n기어·주행 안전 상태는 아직 미연동"
                }
            }
        }
        mobilitySource.start { reading ->
            handler.post {
                if (visible && viewGeneration == generation) {
                    mobilityReading = reading
                    updateMobilityStatus()
                }
            }
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 501 && visible) mobilitySource.enableLocation()
    }

    override fun onStop() {
        visible = false
        appMapDialog?.close()
        appMapDialog = null
        vehicleSource.stop()
        mobilitySource.stop()
        generation++
        handler.removeCallbacks(ticker)
        handler.removeCallbacks(poller)
        super.onStop()
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    private fun renderInitialScreen() {
        val scroll = ScrollView(this)
        scroll.setBackgroundColor(color(R.color.swarm_background))
        // Keep the viewport itself inside the bars, so scrolling cannot put text
        // back under the status bar. Values are absolute, not accumulated padding.
        scroll.setOnApplyWindowInsetsListener { view, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val safe = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
            } else {
                @Suppress("DEPRECATION")
                view.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                    insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
            }
            insets
        }

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(32))
        }
        scroll.addView(content)

        content.addView(label(if (BuildConfig.SUBMISSION_DEMO_ENABLED) "두런두런 · 제출용 가상 시연" else "SWARM MOBILITY OS", 12f, R.color.swarm_primary))
        content.addView(label("협력 주차 어시스턴트", 28f, R.color.swarm_text, true))
        if (BuildConfig.SUBMISSION_DEMO_ENABLED) content.addView(label("시뮬레이션 전용 · 실제 차량 제어 미연결 · 로컬 관제 서버 연결 필요",16f,R.color.swarm_muted))
        content.addView(label(if (BuildConfig.PLEOS_ENABLED) "Pleos 차량 상태 · 로컬 관제" else "로컬 관제 서버 연결 · 차량 정보는 예시 데이터", 14f, R.color.swarm_muted))
        content.addView(Button(this).apply {
            text="협력 평행주차 열기";contentDescription="협력 평행주차 열기"
            isEnabled=BuildConfig.CURB_DEMO_ENABLED
            setOnClickListener { startActivity(android.content.Intent(this@MainActivity,CurbActivity::class.java)) }
        },matchWidth())
        content.addView(label("차량별 이동 요청·동의·진행 확인 · 가상 차량 시뮬레이션",14f,R.color.swarm_muted))
        content.addView(Button(this).apply {
            text="내 차량 참여 요청 열기"
            isEnabled=BuildConfig.CURB_DEMO_ENABLED
            setOnClickListener { startActivity(android.content.Intent(this@MainActivity,ParticipationActivity::class.java)) }
        },matchWidth())
        content.addView(space(16))

        connectionLabel = label("관제 연결 확인 중", 18f, R.color.swarm_primary)
        content.addView(connectionLabel)
        content.addView(space(12))

        content.addView(label("테스트 차량 $vehicleId", 18f, R.color.swarm_text, true))
        vehicleStatus = label("차량 상태 확인 중", 14f, R.color.swarm_muted)
        content.addView(vehicleStatus)
        content.addView(space(18))

        locationStatus = label("위치 확인 중", 15f, R.color.swarm_text)
        navigationStatus = label("내비 연결 확인 중", 15f, R.color.swarm_text)
        content.addView(locationStatus)
        locationPermissionButton = Button(this).apply {
            text = "차량 위치 사용 허용"
            contentDescription = "차량 위치 사용 허용"
            visibility = View.GONE
            setOnClickListener { requestPermissions(arrayOf("pleos.car.permission.FUSED_LOCATION"), 501) }
        }
        content.addView(locationPermissionButton, matchWidth())
        content.addView(navigationStatus)
        positionAlignmentStatus = label("내비 위치 확인 중", 14f, R.color.swarm_muted)
        content.addView(positionAlignmentStatus)
        content.addView(label("연동 테스트 목적지 · 주차 후보와 별개인 SDK 예제 좌표", 13f, R.color.swarm_muted))
        routeButton = Button(this).apply {
            text = "테스트 목적지 안내 요청"
            contentDescription = "테스트 목적지 안내 요청"
            setOnClickListener { mobilitySource.requestDemoRoute() }
        }
        cancelRouteButton = Button(this).apply {
            text = "내비 경로 취소"
            contentDescription = "내비 경로 취소"
            setOnClickListener { mobilitySource.cancelRoute() }
        }
        openNavigationButton = Button(this).apply {
            text = "내비 앱 열기"
            contentDescription = "내비 앱 열기"
            setOnClickListener { mobilitySource.openNavigation() }
        }
        content.addView(routeButton, matchWidth())
        content.addView(cancelRouteButton, matchWidth())
        content.addView(openNavigationButton, matchWidth())
        appMapButton = Button(this).apply {
            text = "앱 지도 열기 · GPS 연동"
            contentDescription = "앱 지도 열기"
            setOnClickListener {
                if (BuildConfig.APP_MAP_ENABLED) {
                    appMapDialog?.close()
                    appMapDialog = AppMapDialog(this@MainActivity,mobilityReading,state,connected,busy,
                        onReserve={ accept(it) },onCancel={ transition("cancel") },onClosed={ appMapDialog=null })
                }
            }
        }
        content.addView(appMapButton,matchWidth())
        content.addView(label("앱 지도는 개발용 미리보기입니다. 표시할 지역은 OpenStreetMap에, 경로 출발·목적지는 경로 보기 시 OSRM에 전달됩니다.",13f,R.color.swarm_muted))
        content.addView(space(12))

        refreshButton = Button(this).apply {
            text = "관제 상태 새로고침"
            contentDescription = "관제 상태 새로고침"
            setOnClickListener { synchronize(); mobilitySource.refresh() }
        }
        content.addView(refreshButton, matchWidth())
        content.addView(space(14))

        eventLog = label("예약 상태 확인 중", 14f, R.color.swarm_muted)
        content.addView(eventLog)
        content.addView(space(16))

        cancelButton = Button(this).apply {
            text = "예약 취소"
            contentDescription = "예약 취소"
            setOnClickListener { transition("cancel") }
        }
        arrivalButton = Button(this).apply {
            text = "주차 도착 확인"
            contentDescription = "주차 도착 확인"
            setOnClickListener { transition("arrival") }
        }
        content.addView(cancelButton, matchWidth())
        content.addView(arrivalButton, matchWidth())
        content.addView(space(12))

        candidateContainer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        content.addView(candidateContainer)

        setContentView(scroll)
        scroll.requestApplyInsets()
    }

    private fun showCandidates() {
        candidateContainer.removeAllViews()
        reservationButtons.clear()
        candidateContainer.addView(label("추천 후보", 20f, R.color.swarm_text, true))
        candidateContainer.addView(space(8))
        state?.candidates?.forEach { candidate ->
            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(16), dp(14), dp(16), dp(14))
                setBackgroundColor(color(R.color.swarm_surface))
            }
            card.addView(label(candidate.spaceId, 20f, R.color.swarm_primary, true))
            card.addView(label("약 ${candidate.etaSeconds / 60}분 · ${when(candidate.status) { "available" -> "예약 가능"; "reserved" -> "예약 중"; else -> "주차 중" }}", 15f, R.color.swarm_text))
            card.addView(label(candidate.reason, 14f, R.color.swarm_muted))
            val acceptButton = Button(this).apply {
                text = "이 후보 예약"
                contentDescription = "${candidate.spaceId} 후보 예약"
                setOnClickListener { accept(candidate) }
            }
            reservationButtons.add(acceptButton to candidate)
            card.addView(acceptButton, matchWidth())
            candidateContainer.addView(card, matchWidth())
            candidateContainer.addView(space(10))
        }
    }

    private fun synchronize() {
        request {
            val pendingSpace = session.pendingSpace
            val pendingRequest = session.pendingRequest
            if (pendingSpace != null && pendingRequest != null) {
                try {
                    client.reserve(pendingSpace, pendingRequest).also { session.clear() }
                } catch (error: ApiFailure) {
                    if (error.status in 400..499) session.clear()
                    throw error
                }
            } else client.state()
        }
    }

    private fun accept(candidate: ParkingCandidate) {
        if (busy || !connected || !candidate.available || state?.reservation?.status == "active") return
        request {
            val requestId = session.begin(candidate.spaceId)
            try {
                client.reserve(session.pendingSpace ?: candidate.spaceId, requestId).also { session.clear() }
            } catch (error: ApiFailure) {
                if (error.status in 400..499) session.clear()
                throw error
            }
        }
    }

    private fun transition(action: String) {
        val reservation = state?.reservation ?: return
        if (busy || !connected || reservation.status != "active") return
        request { client.transition(reservation.id, action) }
    }

    private fun request(operation: () -> CoordinationState) {
        if (busy || !visible) return
        busy = true
        errorMessage = null
        updateStatus()
        val requestGeneration = generation
        worker.execute {
            val result = runCatching(operation)
            handler.post {
                if (!visible || requestGeneration != generation) return@post
                busy = false
                result.onSuccess {
                    state = it
                    connected = true
                    errorMessage = null
                    showCandidates()
                }.onFailure {
                    connected = false
                    errorMessage = when {
                        it is ApiFailure && it.status == 409 -> "다른 차량이 먼저 예약했거나 예약 상태가 변경되었습니다. 다시 확인합니다."
                        session.pendingRequest != null -> "예약 결과 확인 대기 · 연결 복구 후 같은 요청으로 확인합니다."
                        else -> "관제 연결 끊김 · 예약 상태를 확인할 수 없습니다. 운전자 판단으로 이동해 주세요."
                    }
                }
                updateStatus()
            }
        }
    }

    private fun updateMobilityStatus() {
        val reading = mobilityReading
        val fix = reading.position
        locationStatus.text = if (fix == null) reading.locationMessage else {
            val freshness = if (fix.fresh(SystemClock.elapsedRealtime())) "최신 위치" else "오래된 위치 · 현재 위치로 사용하지 마세요."
            String.format(java.util.Locale.US, "%s · %s\n위도 %.6f · 경도 %.6f · 신뢰도 값 %.2f", reading.locationMessage, freshness, fix.latitude, fix.longitude, fix.confidence)
        }
        navigationStatus.text = if (reading.navigationTermsRequired) "내비 앱에서 이용약관 확인이 필요합니다." else reading.navigationMessage
        locationPermissionButton.visibility = if (reading.needsLocationPermission) View.VISIBLE else View.GONE
        val alignment = NavigationPositionCheck.evaluate(fix, reading.navigationPosition, SystemClock.elapsedRealtime())
        positionAlignmentStatus.visibility = if (reading.navigationReady) View.VISIBLE else View.GONE
        positionAlignmentStatus.text = when (alignment) {
            PositionAlignment.WAITING -> "내비 출발 위치 확인 중"
            PositionAlignment.STALE -> "새 위치 수신 대기 · 출발 위치를 다시 확인해 주세요."
            PositionAlignment.DIFFERENT -> "차량 위치와 내비 출발 위치가 다릅니다 · 안내 요청 대기"
            PositionAlignment.MATCHED -> "차량 위치와 내비 출발 위치 일치 확인됨"
        }
        routeButton.isEnabled = reading.navigationReady && !reading.navigationBusy && !reading.navigationTermsRequired && alignment == PositionAlignment.MATCHED
        cancelRouteButton.isEnabled = reading.navigationReady && !reading.navigationBusy && reading.routePresent
        openNavigationButton.isEnabled = reading.navigationReady
        appMapButton.isEnabled = BuildConfig.APP_MAP_ENABLED
        appMapDialog?.update(reading,state,connected,busy)
    }

    private fun updateStatus() {
        updateMobilityStatus()
        connectionLabel.text = when {
            !connected && busy -> "관제 연결 확인 중"
            connected -> "관제 서버 연결됨"
            else -> "관제 연결 끊김"
        }
        refreshButton.isEnabled = !busy
        val reservation = state?.reservation
        val remaining = state?.clock?.remainingMillis(SystemClock.elapsedRealtime()) ?: 0
        val active = reservation?.status == "active"
        reservationButtons.forEach { (button, candidate) ->
            button.isEnabled = connected && !busy && !active && candidate.available && session.pendingRequest == null
        }
        cancelButton.visibility = if (active) View.VISIBLE else View.GONE
        arrivalButton.visibility = if (active) View.VISIBLE else View.GONE
        cancelButton.isEnabled = connected && !busy && remaining > 0
        arrivalButton.isEnabled = connected && !busy && remaining > 0
        val message = errorMessage ?: when {
            !connected -> "관제 상태 확인 중 · 확인 전에는 예약할 수 없습니다."
            active && remaining > 0 -> "${reservation!!.spaceId} 서버 예약 중 · ${(remaining + 999) / 1000}초 후 만료"
            active -> "${reservation!!.spaceId} 만료 시각 도달 · 서버 상태 확인 중"
            reservation?.status == "expired" -> "${reservation.spaceId} 예약 만료 · 후보를 다시 선택해 주세요."
            reservation?.status == "cancelled" -> "${reservation.spaceId} 예약 취소 완료"
            reservation?.status == "arrived" -> "${reservation.spaceId} 주차 도착 반영 완료"
            else -> "예약 가능 · 관제는 안내만 하며 차량을 직접 제어하지 않습니다."
        }
        if (eventLog.text.toString() != message) eventLog.text = message
    }

    private fun panel(title: String, body: String, titleColor: Int): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
            setBackgroundColor(color(R.color.swarm_surface))
            addView(label(title, 18f, titleColor, true))
            addView(label(body, 14f, R.color.swarm_muted))
        }

    private fun label(text: String, size: Float, textColor: Int, bold: Boolean = false): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(color(textColor))
            gravity = Gravity.START
            if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        }

    private fun space(height: Int): View = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(1, dp(height))
    }

    private fun matchWidth(): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun color(id: Int): Int = getColor(id)
}
