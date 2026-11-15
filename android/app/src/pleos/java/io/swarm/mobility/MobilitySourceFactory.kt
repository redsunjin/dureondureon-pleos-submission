package io.swarm.mobility

import android.content.Context

object MobilitySourceFactory {
    fun create(context: Context): MobilitySource = PleosMobilityAdapter(context)
}
