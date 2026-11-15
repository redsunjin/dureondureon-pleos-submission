package io.swarm.mobility

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import ai.pleos.playground.vehicle.Vehicle
import ai.pleos.playground.vehicle.api.model.area.DoorArea
import ai.pleos.playground.vehicle.api.model.capability.AccessType
import ai.pleos.playground.vehicle.constant.area.DoorValue
import ai.pleos.playground.vehicle.constant.area.TurnSignalValue
import ai.pleos.playground.vehicle.listener.DoorPositionListener
import ai.pleos.playground.vehicle.listener.TurnSignalStateListener

/** Read-only vehicle integration. These signals do not establish safe parking. */
class PleosVehicleAdapter(context: Context) : VehicleStateSource {
    private val context = context.applicationContext
    private val handler = Handler(Looper.getMainLooper())
    private val area = DoorArea(DoorValue.ROW_1_LEFT)
    private var vehicle: Vehicle? = null
    private var doorListener: DoorPositionListener? = null
    private var signalListener: TurnSignalStateListener? = null
    private var callback: ((VehicleReading) -> Unit)? = null
    private var generation = 0
    private var doorMessage = "운전석 문 상태 확인 중"
    private var signalMessage = "방향지시등 상태 확인 중"
    private var doorValue: Int? = null
    private var lastObserved: Long? = null
    private val timeout = Runnable {
        if (doorMessage.endsWith("확인 중")) doorMessage = "운전석 문 응답 시간 초과"
        if (signalMessage.endsWith("확인 중")) signalMessage = "방향지시등 응답 시간 초과"
        publish()
    }

    private fun publish() {
        callback?.invoke(VehicleReading("Pleos Vehicle SDK 2.3.2", "$doorMessage\n$signalMessage", doorValue, lastObserved))
    }

    override fun start(onReading: (VehicleReading) -> Unit) {
        stop()
        callback = onReading
        val token = generation
        doorMessage = "운전석 문 상태 확인 중"
        signalMessage = "방향지시등 상태 확인 중"
        doorValue = null
        lastObserved = null
        publish()
        handler.postDelayed(timeout, 10_000)
        fun dispatch(action: () -> Unit) {
            handler.post { if (token == generation && vehicle != null) { action(); publish() } }
        }
        fun doorFailure(error: Exception) = dispatch {
            doorValue = null
            doorMessage = if (error is SecurityException) "운전석 문 조회 권한 없음" else "운전석 문 상태 확인 불가"
            Log.w("SwarmVehicle", "door failure", error)
        }
        fun signalFailure(error: Exception) = dispatch {
            signalMessage = if (error is SecurityException) "방향지시등 조회 권한 없음" else "방향지시등 상태 확인 불가"
            Log.w("SwarmVehicle", "signal failure", error)
        }
        fun doorMeasured(value: Int?, origin: String) = dispatch {
            doorValue = value
            doorMessage = if (value == null) "운전석 문 상태 미제공" else "운전석 문 위치 값: $value"
            if (value != null) lastObserved = System.currentTimeMillis()
            Log.i("SwarmVehicle", "door $origin value=$value")
        }
        fun signalMeasured(value: TurnSignalValue?, origin: String) = dispatch {
            signalMessage = "방향지시등: ${when (value) { TurnSignalValue.LEFT -> "좌측"; TurnSignalValue.RIGHT -> "우측"; TurnSignalValue.OFF -> "꺼짐"; else -> "미제공" }}"
            if (value != null) lastObserved = System.currentTimeMillis()
            Log.i("SwarmVehicle", "turnSignal $origin value=$value")
        }
        try {
            val connection = Vehicle(context)
            vehicle = connection
            connection.initialize()
            Log.i("SwarmVehicle", "initialize returned; waiting for capability/read evidence")
            val door = connection.getDoor()
            door.checkDoorPositionCapabilities(
                onComplete = { config, bounds -> dispatch {
                    Log.i("SwarmVehicle", "door capability access=${config.accessType}, area=${bounds[area]}")
                    if (config.accessType !in setOf(AccessType.READ, AccessType.READ_WRITE) || bounds[area] == null) {
                        doorMessage = "운전석 문 읽기 미지원"
                    } else {
                        val updates = object : DoorPositionListener {
                            override fun onPositionUpdated(pos: Int?) = doorMeasured(pos, "callback")
                            override fun onFailed(exception: Exception) = doorFailure(exception)
                        }
                        doorListener = updates
                        try {
                            door.registerDoorPosition(area, updates)
                            door.getCurrentDoorPosition(area = area, onComplete = { doorMeasured(it, "get") }, onFailed = { doorFailure(it) })
                        } catch (error: Exception) { doorFailure(error) }
                    }
                } }, onFailed = { doorFailure(it) },
            )
            val signal = connection.getTurnSignal()
            signal.checkTurnSignalStateCapabilities(
                onComplete = { config -> dispatch {
                    Log.i("SwarmVehicle", "turnSignal capability access=${config.accessType}")
                    if (config.accessType !in setOf(AccessType.READ, AccessType.READ_WRITE)) {
                        signalMessage = "방향지시등 읽기 미지원"
                    } else {
                        val updates = object : TurnSignalStateListener {
                            override fun onTurnSignalStateUpdated(state: TurnSignalValue?) = signalMeasured(state, "callback")
                            override fun onFailed(exception: Exception) = signalFailure(exception)
                        }
                        signalListener = updates
                        try {
                            signal.registerTurnSignalState(updates)
                            signal.getCurrentTurnSignalState(onComplete = { signalMeasured(it, "get") }, onFailed = { signalFailure(it) })
                        } catch (error: Exception) { signalFailure(error) }
                    }
                } }, onFailed = { signalFailure(it) },
            )
        } catch (error: Exception) {
            doorMessage = "차량 서비스 연결 불가"
            signalMessage = "Pleos 차량 환경을 확인해 주세요."
            Log.w("SwarmVehicle", "initialize failed", error)
            publish()
        }
    }

    override fun stop() {
        generation++
        handler.removeCallbacks(timeout)
        callback = null
        val connection = vehicle
        val doorUpdates = doorListener
        val signalUpdates = signalListener
        vehicle = null
        doorListener = null
        signalListener = null
        if (connection != null) {
            if (doorUpdates != null) runCatching { connection.getDoor().unregisterDoorPosition(area, doorUpdates) }
                .onFailure { Log.w("SwarmVehicle", "door unsubscribe failed", it) }
            if (signalUpdates != null) runCatching { connection.getTurnSignal().unregisterTurnSignalState(signalUpdates) }
                .onFailure { Log.w("SwarmVehicle", "signal unsubscribe failed", it) }
            runCatching { connection.release() }.onFailure { Log.w("SwarmVehicle", "release failed", it) }
            Log.i("SwarmVehicle", "subscriptions removed; session released")
        }
    }
}
