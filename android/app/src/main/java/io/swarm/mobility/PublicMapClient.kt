package io.swarm.mobility

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.Properties
import java.util.concurrent.ConcurrentHashMap

/** Debug PoC providers. Routes/precise origins stay in memory; only visible map tiles are cached. */
class PublicMapClient(private val cache: File) {
    companion object {
        private val routeLock = Any()
        private var lastRouteAt = -1100L
    }
    private val connections = ConcurrentHashMap.newKeySet<HttpURLConnection>()
    @Volatile private var closed = false
    private fun connect(url: String): HttpURLConnection {
        if (closed) throw IOException("closed")
        val target = URL(url)
        require(target.protocol == "https")
        val c = target.openConnection() as HttpURLConnection
        c.connectTimeout = 6000; c.readTimeout = 8000; c.instanceFollowRedirects = false; c.useCaches = false
        c.setRequestProperty("User-Agent", "SwarmMobilityOS-PleosPoC/0.1")
        connections.add(c)
        if (closed) { finish(c); throw IOException("closed") }
        return c
    }
    private fun finish(c: HttpURLConnection) { connections.remove(c); c.disconnect() }
    private fun bytes(c: HttpURLConnection, max: Int): ByteArray = c.inputStream.use { input ->
        val out = java.io.ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        while (true) {
            val n = input.read(buffer); if (n < 0) break
            if (out.size() + n > max) throw IOException("response too large")
            out.write(buffer,0,n)
        }
        out.toByteArray()
    }
    fun tile(key: MapTile): Bitmap {
        cache.mkdirs()
        val file = File(cache,"${key.zoom}-${key.x}-${key.y}.png")
        val metadata = File(cache,"${key.zoom}-${key.x}-${key.y}.properties")
        val properties = Properties().apply { if (metadata.exists()) runCatching { metadata.inputStream().use { load(it) } } }
        val now = System.currentTimeMillis()
        if (file.exists() && (properties.getProperty("expires")?.toLongOrNull() ?: 0) > now) {
            BitmapFactory.decodeFile(file.path)?.let { return it }
        }
        val c = connect(BuildConfig.MAP_TILE_BASE_URL.trimEnd('/')+"/${key.zoom}/${key.x}/${key.y}.png")
        try {
            if (file.exists()) {
                properties.getProperty("etag")?.let { c.setRequestProperty("If-None-Match",it) }
                properties.getProperty("modified")?.let { c.setRequestProperty("If-Modified-Since",it) }
            }
            val status = c.responseCode
            if (status != 200 && status != 304) throw IOException("tile HTTP $status")
            val cacheControl = c.getHeaderField("Cache-Control") ?: ""
            val noStore = cacheControl.contains("no-store",ignoreCase=true)
            val bitmap = if (status == 304) BitmapFactory.decodeFile(file.path) else {
                val raw = bytes(c,1_048_576)
                val decoded = BitmapFactory.decodeByteArray(raw,0,raw.size) ?: throw IOException("invalid tile")
                if (decoded.width != 256 || decoded.height != 256) { decoded.recycle(); throw IOException("invalid tile size") }
                if (!noStore) File(cache,file.name+".tmp").apply { writeBytes(raw); if (!renameTo(file)) throw IOException("tile cache") }
                decoded
            } ?: throw IOException("missing tile")
            c.getHeaderField("ETag")?.let { properties.setProperty("etag",it) }
            c.getHeaderField("Last-Modified")?.let { properties.setProperty("modified",it) }
            val maxAge = Regex("max-age=(\\d+)").find(c.getHeaderField("Cache-Control") ?: "")?.groupValues?.get(1)?.toLongOrNull()
            val expiry = if (cacheControl.contains("no-cache",ignoreCase=true)) now else maxAge?.let { now + it.coerceAtMost(31_536_000)*1000 }
                ?: c.getHeaderFieldDate("Expires",0).takeIf { it > now } ?: now + 7*24*60*60*1000L
            properties.setProperty("expires",expiry.toString())
            if (noStore) { file.delete();metadata.delete() } else metadata.outputStream().use { properties.store(it,null) }
            return bitmap
        } finally { finish(c) }
    }
    fun route(origin: GeoPoint, destination: GeoPoint): RoutePreview {
        require(origin.valid() && destination.valid())
        val url = BuildConfig.MAP_ROUTE_BASE_URL.trimEnd('/')+"/route/v1/driving/${origin.longitude},${origin.latitude};${destination.longitude},${destination.latitude}?overview=full&geometries=geojson&alternatives=false&generate_hints=false&radiuses=300;300"
        synchronized(routeLock) {
            val wait = 1100 - (android.os.SystemClock.elapsedRealtime() - lastRouteAt)
            if (wait > 0) Thread.sleep(wait)
            if (closed) throw IOException("closed")
            lastRouteAt = android.os.SystemClock.elapsedRealtime()
        }
        val c = connect(url)
        try {
            if (c.responseCode != 200) throw IOException("route HTTP ${c.responseCode}")
            val json = JSONObject(String(bytes(c,2_000_000),Charsets.UTF_8))
            if (json.optString("code") != "Ok") throw IOException("route unavailable")
            val route = json.getJSONArray("routes").getJSONObject(0)
            val geometry = route.getJSONObject("geometry")
            if (geometry.getString("type") != "LineString") throw IOException("invalid geometry")
            val coordinates = geometry.getJSONArray("coordinates")
            if (coordinates.length() !in 2..20_000) throw IOException("invalid geometry size")
            val points = (0 until coordinates.length()).map { i -> coordinates.getJSONArray(i).let { GeoPoint(it.getDouble(1),it.getDouble(0)) } }
            val snap = json.getJSONArray("waypoints").getJSONObject(0).getJSONArray("location").let { GeoPoint(it.getDouble(1),it.getDouble(0)) }
            return RoutePreview(origin,destination,snap,points,route.getDouble("distance"),route.getDouble("duration")).also {
                if (!it.valid()) throw IOException("route endpoints differ")
            }
        } finally { finish(c) }
    }
    fun close() { closed = true; connections.toList().forEach { finish(it) } }
}
