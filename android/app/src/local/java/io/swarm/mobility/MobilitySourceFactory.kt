package io.swarm.mobility

import android.content.Context

object MobilitySourceFactory {
    fun create(context: Context): MobilitySource = object : MobilitySource {
        override fun start(onReading: (MobilityReading) -> Unit) {
            onReading(MobilityReading("이 빌드에서는 차량 위치를 사용하지 않습니다.", navigationMessage = "Pleos 내비 연동 빌드가 필요합니다."))
        }
        override fun enableLocation() = Unit
        override fun refresh() = Unit
        override fun requestDemoRoute() = Unit
        override fun cancelRoute() = Unit
        override fun openNavigation() = Unit
        override fun stop() = Unit
    }
}
