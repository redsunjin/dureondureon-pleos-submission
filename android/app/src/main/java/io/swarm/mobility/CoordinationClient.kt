package io.swarm.mobility

import android.os.SystemClock
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

class ApiFailure(val status: Int, val code: String) : IOException(code)

data class ParkingCandidate(val spaceId: String, val etaSeconds: Int, val reason: String,
    val available: Boolean, val status: String, val mapPosition: GeoPoint? = null,
    val coordinateSource: String? = null)
data class ServerReservation(val id: String, val spaceId: String, val status: String,
    val expiresAtMillis: Long, val requestId: String)
data class CoordinationState(val candidates: List<ParkingCandidate>, val reservation: ServerReservation?,
    val clock: ReservationClock)

class CoordinationClient(private val baseUrl: String, private val vehicleId: String) {
    fun state(): CoordinationState = request("GET", "/v1/state?vehicleId=${URLEncoder.encode(vehicleId, "UTF-8")}")

    fun reserve(spaceId: String, requestId: String): CoordinationState = request("POST", "/v1/reservations",
        JSONObject().put("vehicleId", vehicleId).put("spaceId", spaceId).put("requestId", requestId))

    fun transition(id: String, action: String): CoordinationState = request("POST", "/v1/reservations/$id/$action",
        JSONObject().put("vehicleId", vehicleId))

    private fun request(method: String, path: String, body: JSONObject? = null): CoordinationState {
        val started = SystemClock.elapsedRealtime()
        val connection = URL(baseUrl + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = 2500
            connection.readTimeout = 2500
            connection.useCaches = false
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val raw = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: "{}"
            if (status !in 200..299) throw ApiFailure(status, JSONObject(raw).optString("error", "server_error"))
            val json = JSONObject(raw)
            val list = json.getJSONArray("candidates")
            val candidates = (0 until list.length()).map { index ->
                val c = list.getJSONObject(index)
                ParkingCandidate(c.getString("spaceId"), c.getInt("etaSeconds"), c.getString("reason"),
                    c.getBoolean("available"), c.getString("status"),
                    c.optJSONObject("mapPosition")?.let { GeoPoint(it.optDouble("latitude"), it.optDouble("longitude")) }?.takeIf { it.valid() },
                    c.optJSONObject("mapPosition")?.optString("source"))
            }
            val reservation = json.optJSONObject("reservation")?.let {
                ServerReservation(it.getString("reservationId"), it.getString("spaceId"), it.getString("status"),
                    it.getLong("expiresAtMillis"), it.getString("requestId"))
            }
            val received = SystemClock.elapsedRealtime()
            // Account for the full request round trip conservatively. The server
            // remains authoritative; a local zero triggers refresh, not a new lease.
            val remaining = if (reservation?.status == "active")
                reservation.expiresAtMillis - json.getLong("serverTimeMillis") - (received - started) else 0
            return CoordinationState(candidates, reservation, ReservationClock(remaining.coerceAtLeast(0), received))
        } finally {
            connection.disconnect()
        }
    }
}
