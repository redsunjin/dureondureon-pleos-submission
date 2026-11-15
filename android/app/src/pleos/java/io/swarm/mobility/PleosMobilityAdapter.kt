package io.swarm.mobility

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import ai.pleos.playground.location.fused.FusedLocation
import ai.pleos.playground.location.fused.listener.FusedLocationListener
import ai.pleos.playground.location.fused.model.LocationData
import ai.pleos.playground.navi.helper.NaviHelper
import ai.pleos.playground.navi.helper.listener.NaviHelperEventListener
import ai.pleos.playground.navi.constants.NaviErrorCode
import ai.pleos.playground.navi.constants.NaviMode
import ai.pleos.playground.navi.constants.NavigationStatus
import ai.pleos.playground.navi.data.CurrentLocationInfo
import ai.pleos.playground.navi.data.DestinationInfo
import ai.pleos.playground.navi.data.RouteInfo
import ai.pleos.playground.navi.data.RouteStartInfo
import ai.pleos.playground.navi.data.RouteStateInfo

class PleosMobilityAdapter(context: Context) : MobilitySource {
    private val context = context.applicationContext
    private val handler = Handler(Looper.getMainLooper())
    private var callback: ((MobilityReading) -> Unit)? = null
    private var generation = 0
    private var fused: FusedLocation? = null
    private var locationListener: FusedLocationListener? = null
    private var navi: NaviHelper? = null
    private var naviListener: NaviHelperEventListener? = null
    private var reading = MobilityReading("위치 확인 중")
    private var progress = RouteProgress()
    private var routeRequested = false
    private var guiding = false
    private val positionPoller = object : Runnable {
        override fun run() {
            if (callback != null && reading.navigationReady) {
                if (canReadPosition()) {
                    runCatching { navi?.getCurrentLocationInfo() }.onFailure {
                        update(reading.copy(navigationPosition = null))
                    }
                } else update(reading.copy(position = null, navigationPosition = null))
                handler.postDelayed(this, 3000)
            }
        }
    }
    private val routeTimeout = Runnable {
        if (reading.navigationBusy) {
            progress.failed()
            routeRequested = false
            update(reading.copy(navigationBusy = false, navigationMessage = "내비 응답 시간 초과 · 목적지를 다시 확인해 주세요."))
        }
    }
    private val locationTimeout = Runnable {
        if (reading.position == null && fused != null) update(reading.copy(locationMessage = "위치 응답 대기 중 · 수신 전에는 현재 위치를 표시하지 않습니다."))
    }

    private fun canReadPosition() = context.checkSelfPermission(LOCATION_PERMISSION) == PackageManager.PERMISSION_GRANTED

    private fun update(next: MobilityReading) { reading = next; callback?.invoke(next) }
    private fun dispatch(token: Int, action: () -> Unit) {
        handler.post { if (token == generation && callback != null) action() }
    }

    override fun start(onReading: (MobilityReading) -> Unit) {
        stop()
        callback = onReading
        reading = MobilityReading("위치 확인 중")
        progress = RouteProgress()
        routeRequested = false
        guiding = false
        update(reading)
        enableLocation()
        val token = generation
        try {
            val helper = NaviHelper(context)
            navi = helper
            val listener = object : NaviHelperEventListener {
                override fun onCurrentLocationInfo(currentLocationInfo: CurrentLocationInfo) = dispatch(token) {
                    if (!canReadPosition()) return@dispatch
                    val position = NavigationPosition(currentLocationInfo.latitude, currentLocationInfo.longitude, SystemClock.elapsedRealtime())
                    if (BuildConfig.DEBUG) Log.i("SwarmMobility", "navigation position lat=${position.latitude} lon=${position.longitude}")
                    update(reading.copy(navigationPosition = position.takeIf { it.valid() }))
                }
                override fun onRouteStarted(routeStartInfo: RouteStartInfo) = dispatch(token) {
                    Log.i("SwarmMobility", "routeStarted name=${routeStartInfo.name}")
                    update(reading.copy(navigationMessage = "경로 탐색 시작 · ${routeStartInfo.name}"))
                }
                override fun onRoutingCompleted() = dispatch(token) {
                    Log.i("SwarmMobility", "routingCompleted")
                    if (!routeRequested) return@dispatch
                    progress.routingCompleted()
                    helper.getDestinationInfo()
                    update(reading.copy(navigationMessage = if (progress.verified) "테스트 목적지 경로 탐색 완료" else "경로 탐색 완료 · 목적지 확인 중", navigationBusy = progress.pending))
                }
                override fun onDestinationChanged() = dispatch(token) {
                    Log.i("SwarmMobility", "destinationChanged")
                    helper.getDestinationInfo()
                }
                override fun onDestinationInfo(destinationInfo: DestinationInfo) = dispatch(token) {
                    if (BuildConfig.DEBUG) Log.i("SwarmMobility", "destinationInfo lat=${destinationInfo.latitude} lon=${destinationInfo.longitude} name=${destinationInfo.name}")
                    if (progress.cancelled && destinationInfo.name.isNotBlank()) return@dispatch
                    progress.confirmDestination(destinationInfo.latitude, destinationInfo.longitude)
                    if (progress.verified) handler.removeCallbacks(routeTimeout)
                    val name = destinationInfo.name.ifBlank { "없음" }
                    update(reading.copy(routePresent = name != "없음", navigationBusy = progress.pending,
                        navigationMessage = when {
                            progress.verified -> "테스트 목적지 경로 탐색 완료 · $name"
                            guiding && progress.destinationMatched -> "안내 중 · 테스트 목적지 좌표 확인됨"
                            guiding -> "안내 중 · $name (테스트 요청 좌표와 다름)"
                            routeRequested && progress.destinationMatched -> "테스트 목적지 확인됨 · 경로 완료 응답 대기"
                            else -> "현재 내비 목적지: $name"
                        }))
                }
                override fun onRouteCancelled() = dispatch(token) {
                    Log.i("SwarmMobility", "routeCancelled")
                    handler.removeCallbacks(routeTimeout)
                    progress.cancelAcknowledged()
                    routeRequested = false
                    guiding = false
                    update(reading.copy(routePresent = false, navigationBusy = false, navigationMessage = "내비 경로 취소 확인됨"))
                }
                override fun onRouteStateInfo(routeStateInfo: RouteStateInfo) = dispatch(token) {
                    Log.i("SwarmMobility", "routeStateInfo terms=${routeStateInfo.termsAgreement} routing=${routeStateInfo.isRouting} mode=${routeStateInfo.naviMode}")
                    guiding = routeStateInfo.naviMode == NaviMode.ROUTE_GUIDING || routeStateInfo.naviMode == NaviMode.SIMULATION_GUIDING
                    update(reading.copy(navigationTermsRequired = !routeStateInfo.termsAgreement))
                    helper.getDestinationInfo()
                }
                override fun onNavigationStatus(navigationStatus: NavigationStatus) = dispatch(token) {
                    Log.i("SwarmMobility", "navigationStatus=$navigationStatus")
                }
                override fun onError(errorCode: NaviErrorCode) = dispatch(token) {
                    Log.w("SwarmMobility", "navi error=$errorCode")
                    handler.removeCallbacks(routeTimeout)
                    progress.failed()
                    routeRequested = false
                    update(reading.copy(navigationBusy = false, navigationMessage = "경로 요청 처리 실패 · 내비 상태를 확인해 주세요."))
                }
            }
            naviListener = listener
            helper.initialize(onConnected = { dispatch(token) {
                helper.addListener(listener)
                Log.i("SwarmMobility", "navi connected")
                update(reading.copy(navigationReady = true, navigationMessage = "내비 서비스 연결됨"))
                queryNavigation()
                positionPoller.run()
            } }, onFailed = { error -> dispatch(token) {
                Log.w("SwarmMobility", "navi initialization failed", error)
                update(reading.copy(navigationReady = false, navigationMessage = "내비 서비스에 연결할 수 없습니다."))
            } })
        } catch (error: Exception) {
            Log.w("SwarmMobility", "navi startup failed", error)
            update(reading.copy(navigationReady = false, navigationMessage = "내비 서비스를 사용할 수 없습니다."))
        }
    }

    override fun enableLocation() {
        val token = generation
        if (fused != null) return
        try {
            val source = FusedLocation(context)
            if (!source.isSupported()) {
                update(reading.copy(position = null, needsLocationPermission = false, locationMessage = "이 환경은 Pleos 차량 위치를 지원하지 않습니다."))
                Log.i("SwarmMobility", "fused supported=false")
                return
            }
            Log.i("SwarmMobility", "fused supported=true")
            if (context.checkSelfPermission(LOCATION_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
                update(reading.copy(position = null, needsLocationPermission = true, locationMessage = "차량 위치 사용 허용이 필요합니다."))
                return
            }
            fused = source
            update(reading.copy(needsLocationPermission = false, locationMessage = "위치 수신 대기 중"))
            val listener = object : FusedLocationListener {
                override fun onChanged(location: LocationData?) = dispatch(token) {
                    handler.removeCallbacks(locationTimeout)
                    val fix = location?.let { PositionFix(it.latitude, it.longitude, it.confidence, it.timestampMillis, System.currentTimeMillis(), SystemClock.elapsedRealtime()) }
                    val valid = fix?.takeIf { it.valid() }
                    // No location history is persisted or uploaded; coordinates are logged only in debug builds.
                    if (BuildConfig.DEBUG) Log.i("SwarmMobility", "fused changed lat=${location?.latitude} lon=${location?.longitude} time=${location?.timestampMillis}")
                    update(reading.copy(position = valid, locationMessage = when { location == null -> "위치 정보 없음"; valid == null -> "유효하지 않은 위치 수신"; else -> "Pleos 차량 위치" }))
                }
                override fun onError(exception: Exception) = dispatch(token) {
                    handler.removeCallbacks(locationTimeout)
                    Log.w("SwarmMobility", "fused failure", exception)
                    update(reading.copy(position = null, locationMessage = "차량 위치 접근 불가 · 권한 또는 차량 개인정보 설정을 확인해 주세요."))
                }
            }
            locationListener = listener
            source.initialize()
            source.registerFusedLocationCallback(listener)
            source.getLastKnownLocation(listener)
            handler.postDelayed(locationTimeout, 10_000)
        } catch (error: Exception) {
            Log.w("SwarmMobility", "fused startup failed", error)
            update(reading.copy(position = null, locationMessage = "차량 위치 서비스를 사용할 수 없습니다."))
        }
    }

    private fun queryNavigation() {
        if (!reading.navigationReady) return
        runCatching { navi?.getRouteStateInfo(); navi?.getDestinationInfo(); if (canReadPosition()) navi?.getCurrentLocationInfo() }.onFailure {
            Log.w("SwarmMobility", "navi query failed", it)
            update(reading.copy(navigationMessage = "내비 정보 조회 실패"))
        }
    }

    override fun refresh() {
        enableLocation()
        runCatching { locationListener?.let { fused?.getLastKnownLocation(it) } }.onFailure {
            update(reading.copy(position = null, locationMessage = "차량 위치 조회 실패 · 권한을 확인해 주세요."))
        }
        queryNavigation()
    }

    override fun requestDemoRoute() {
        if (!reading.navigationReady || reading.navigationBusy || reading.navigationTermsRequired) return
        if (NavigationPositionCheck.evaluate(reading.position, reading.navigationPosition, SystemClock.elapsedRealtime()) != PositionAlignment.MATCHED) {
            update(reading.copy(navigationMessage = "차량과 내비의 위치가 일치한 뒤 안내를 요청해 주세요."))
            return
        }
        routeRequested = true
        progress.requested()
        update(reading.copy(navigationBusy = true, navigationMessage = "테스트 목적지 요청 전송 · 경로 응답 대기"))
        handler.postDelayed(routeTimeout, 30_000)
        try {
            navi?.requestRoute(RouteInfo(latitude = DemoDestination.LATITUDE, longitude = DemoDestination.LONGITUDE,
                // Empty POI identifiers preserve the coordinate target; the documentation
                // example POI ID resolved to another location in the installed navigation catalog.
                displayLongitude = null, displayLatitude = null, poiName = DemoDestination.NAME,
                poiId = "", poiSubId = "", address = ""))
            Log.i("SwarmMobility", "requestRoute invoked for official demo POI; awaiting service evidence")
        } catch (error: Exception) {
            handler.removeCallbacks(routeTimeout)
            progress.failed()
            routeRequested = false
            Log.w("SwarmMobility", "route request failed", error)
            update(reading.copy(navigationBusy = false, navigationMessage = "목적지 요청 실패"))
        }
    }

    override fun cancelRoute() {
        if (!reading.navigationReady || reading.navigationBusy) return
        progress.cancelRequested()
        update(reading.copy(navigationBusy = true, navigationMessage = "내비 경로 취소 응답 대기"))
        handler.postDelayed(routeTimeout, 30_000)
        runCatching { navi?.cancelRoute() }.onFailure {
            handler.removeCallbacks(routeTimeout)
            progress.failed()
            update(reading.copy(navigationBusy = false, navigationMessage = "내비 경로 취소 요청 실패"))
        }
    }

    override fun openNavigation() {
        val intent = context.packageManager.getLaunchIntentForPackage("ai.umos.maps.android.navigation.app")
        if (intent == null) update(reading.copy(navigationMessage = "내비 앱을 찾을 수 없습니다."))
        else context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    override fun stop() {
        generation++
        callback = null
        handler.removeCallbacks(routeTimeout)
        handler.removeCallbacks(positionPoller)
        handler.removeCallbacks(locationTimeout)
        locationListener?.let { listener -> runCatching { fused?.unregisterFusedLocationCallback(listener) } }
        runCatching { fused?.release() }
        fused = null
        locationListener = null
        naviListener?.let { listener -> runCatching { navi?.removeListener(listener) } }
        runCatching { navi?.release() }
        navi = null
        naviListener = null
        Log.i("SwarmMobility", "location/navigation listeners removed; SDK sessions released")
    }

    companion object { const val LOCATION_PERMISSION = "pleos.car.permission.FUSED_LOCATION" }
}
