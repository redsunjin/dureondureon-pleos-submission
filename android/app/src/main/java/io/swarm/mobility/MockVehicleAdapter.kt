package io.swarm.mobility

data class VehicleSnapshot(
    val vehicleId: String,
    val zoneId: String,
    val heading: String,
    val intent: String,
    val adapter: String,
)

/**
 * Adapter boundary for the future Pleos Vehicle SDK.
 * The mock keeps the UI and coordination contract testable before SDK access.
 */
class MockVehicleAdapter {
    fun snapshot(): VehicleSnapshot = VehicleSnapshot(
        vehicleId = "V-07",
        zoneId = "Parking A 진입",
        heading = "동쪽",
        intent = "주차 후보 탐색",
        adapter = "MOCK / Pleos adapter boundary",
    )
}
