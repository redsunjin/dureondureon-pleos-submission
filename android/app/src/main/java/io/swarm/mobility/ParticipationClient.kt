package io.swarm.mobility

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

class ParticipationClient(private val base: String) {
    fun call(path: String,body: JSONObject): JSONObject {
        require(path in setOf("poll","respond"))
        val c=URL("$base/v1/curb/participant/$path").openConnection() as HttpURLConnection
        try {
            c.requestMethod="POST";c.connectTimeout=2000;c.readTimeout=2000;c.useCaches=false;c.doOutput=true
            c.setRequestProperty("Content-Type","application/json")
            c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code=c.responseCode
            val json=JSONObject((if(code in 200..299)c.inputStream else c.errorStream)?.bufferedReader()?.use { it.readText() } ?: "{}")
            if(code !in 200..299)throw ApiFailure(code,json.optString("error","server_error"))
            val s=json.getJSONObject("session");val p=json.getJSONObject("participant")
            if(!s.getBoolean("demo") || s.getInt("revision")<1 || p.getString("moveRequestId")!="${s.getString("sessionId")}_${p.getString("vehicleId")}" ||
                s.getString("status") !in setOf("awaiting","ready","running","stopped","expired","rejected","completed") ||
                p.getString("decision") !in setOf("pending","approved","rejected","withdrawn"))throw IOException("invalid participant snapshot")
            return json
        } finally { c.disconnect() }
    }
}
