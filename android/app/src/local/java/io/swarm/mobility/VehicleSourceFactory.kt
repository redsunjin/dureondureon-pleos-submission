package io.swarm.mobility

import android.content.Context

object VehicleSourceFactory {
    fun create(context: Context): VehicleStateSource = object : VehicleStateSource {
        override fun start(onReading: (VehicleReading) -> Unit) {
            onReading(VehicleReading("예시 차량", "차량 SDK 미사용 · 실제 차량 상태는 수신하지 않습니다."))
        }
        override fun stop() = Unit
    }
}
