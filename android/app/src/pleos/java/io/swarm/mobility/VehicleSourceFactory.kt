package io.swarm.mobility

import android.content.Context

object VehicleSourceFactory {
    fun create(context: Context): VehicleStateSource = PleosVehicleAdapter(context)
}
