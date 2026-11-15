package io.swarm.mobility

/** Vehicle state is distinct from the demo vehicle ID used by the coordination API. */
data class VehicleReading(
    val source: String,
    val message: String,
    val driverDoorPosition: Int? = null,
    val observedAtMillis: Long? = null,
)

interface VehicleStateSource {
    fun start(onReading: (VehicleReading) -> Unit)
    fun stop()
}
