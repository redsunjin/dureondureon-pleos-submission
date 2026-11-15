package io.swarm.mobility

import android.os.SystemClock
import org.json.JSONObject
import org.json.JSONArray
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

class CurbClient(private val base: String) {
    fun state()=request("GET","/v1/curb/state?ownerVehicleId=V-07")
    fun submit(payload: JSONObject): CurbSnapshot {
        val action=payload.getString("action")
        val path=when(action) {
            "create" -> "/v1/curb/sessions"
            "replan" -> "/v1/curb/sessions/${payload.getString("sessionId")}/replan"
            "observe" -> "/v1/curb/sessions/${payload.getString("sessionId")}/observations"
            "traffic" -> "/v1/curb/sessions/${payload.getString("sessionId")}/traffic"
            else -> "/v1/curb/sessions/${payload.getString("sessionId")}/actions"
        }
        return request("POST",path,payload)
    }
    companion object {
        fun payload(action: String, session: CurbSession?, car: CurbVehicle?): JSONObject = JSONObject().apply {
            put("ownerVehicleId","V-07");put("requestId",UUID.randomUUID().toString());put("action",action)
            if(session!=null) {put("sessionId",session.id);put("revision",session.revision)}
            if(car!=null) {put("participantId",car.id);put("moveRequestId",car.moveRequestId)}
        }
        fun observation(session: CurbSession, kind: String): JSONObject = payload("observe",session,null).apply {
            fun pose(p: CurbPose)=JSONObject().put("x",p.x).put("y",p.y).put("yaw",p.yaw)
            when(kind) {
                "vehicle" -> {
                    val own=session.vehicles.single { it.id=="NEW" }
                    put("vehiclePoses",JSONArray().put(JSONObject().put("vehicleId",own.id).put("pose",pose(own.pose.copy(y=own.pose.y+.15)))))
                }
                "obstacle","clear" -> {
                    val items=JSONArray()
                    if(kind=="obstacle") {
                        session.obstacles.filter { it.id!="DEMO-BOX" }.forEach { items.put(JSONObject().put("id",it.id).put("pose",pose(it.pose)).put("length",it.length).put("width",it.width)) }
                        items.put(JSONObject().put("id","DEMO-BOX").put("pose",pose(CurbPose(16.0,5.9,0.0))).put("length",.25).put("width",.25))
                    }
                    put("obstacles",items)
                }
            }
        }
        fun traffic(session: CurbSession,scenario: String)=payload("traffic",session,null).apply {
            put("source","scripted");put("scenario",scenario)
        }
        private fun pose(p: JSONObject)=CurbPose(p.getDouble("x"),p.getDouble("y"),p.getDouble("yaw"))
        private fun traffic(t: JSONObject?): CurbTraffic {
            if(t==null)return CurbTraffic()
            val a=t.getJSONArray("tracks")
            return CurbTraffic(t.getString("safety"),t.optString("source").takeUnless { it=="null" || it.isEmpty() },
                (0 until a.length()).map { i->a.getJSONObject(i).let { o->CurbMovingObstacle(o.getString("id"),o.getString("kind"),
                    pose(o.getJSONObject("pose")),pose(o.getJSONObject("predictedPose")),o.getDouble("length"),o.getDouble("width"),o.getDouble("vx"),o.getDouble("vy")) } },
                t.getBoolean("safeToReplan"),t.getDouble("horizonSeconds"),t.getLong("clearRemainingMillis"),
                t.optString("riskId").takeUnless { it=="null" || it.isEmpty() },
                if(t.isNull("timeToRiskSeconds"))null else t.getDouble("timeToRiskSeconds"))
        }
    }
    private fun request(method: String,path: String,payload: JSONObject?=null): CurbSnapshot {
        val start=SystemClock.elapsedRealtime()
        val connection=URL(base+path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod=method;connection.connectTimeout=2500;connection.readTimeout=if(path.endsWith("/replan"))15000 else 2500
            connection.useCaches=false
            if(payload!=null) {
                connection.doOutput=true;connection.setRequestProperty("Content-Type","application/json")
                connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code=connection.responseCode
            val stream=if(code in 200..299)connection.inputStream else connection.errorStream
            val json=JSONObject(stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: "{}")
            if(code !in 200..299)throw ApiFailure(code,json.optString("error","server_error"))
            val received=SystemClock.elapsedRealtime()
            val session=json.optJSONObject("session")?.let { s ->
                if(!s.getBoolean("demo"))throw IOException("non-demo session rejected")
                val array=s.getJSONArray("participants")
                val cars=(0 until array.length()).map { index ->
                    val c=array.getJSONObject(index);val p=c.getJSONObject("pose");val steps=c.getJSONArray("steps")
                    CurbVehicle(c.getString("vehicleId"),c.getString("role"),c.getDouble("length"),c.getDouble("width"),
                        CurbPose(p.getDouble("x"),p.getDouble("y"),p.getDouble("yaw")),c.getString("moveRequestId"),
                        c.getString("decision"),c.getString("motionState"),c.getDouble("progressPercent"),c.getDouble("totalTravel"),
                        (0 until steps.length()).map { steps.getJSONObject(it).let { st->CurbStep(st.getString("stage"),st.getDouble("distance")) } })
                }
                CurbSession(s.getString("sessionId"),s.getInt("revision"),s.getString("status"),s.getString("reason"),
                    s.getInt("playbackRate"),s.getDouble("sceneLength"),s.getDouble("roadWidth"),s.getDouble("initialGap"),
                    s.getDouble("openingGap"),s.getDouble("elapsedSeconds"),s.getDouble("durationSeconds"),s.getString("stage"),
                    s.optString("actor").takeUnless { it=="null" || it.isEmpty() },cars,
                    ReservationClock((s.getLong("expiresAtMillis")-json.getLong("serverTimeMillis")-(received-start)).coerceAtLeast(0),received),
                    s.optInt("replanGeneration",0),s.optString("parentSessionId").takeUnless { it=="null" || it.isEmpty() },
                    s.optInt("parentRevision",0).takeIf { it>0 },s.optString("replanMode").takeUnless { it=="null" || it.isEmpty() },
                    s.optString("checkpointHash").takeUnless { it=="null" || it.isEmpty() },s.optInt("environmentRevision",0),
                    s.optJSONArray("obstacles")?.let { a->(0 until a.length()).map { i->a.getJSONObject(i).let { o->
                        val p=o.getJSONObject("pose");CurbObstacle(o.getString("id"),CurbPose(p.getDouble("x"),p.getDouble("y"),p.getDouble("yaw")),o.getDouble("length"),o.getDouble("width")) } } } ?: emptyList(),
                    s.optJSONArray("plannedPath")?.let { a->(0 until a.length()).map { i->a.getJSONObject(i).let { p->CurbPose(p.getDouble("x"),p.getDouble("y"),p.getDouble("yaw")) } } } ?: emptyList(),traffic(s.optJSONObject("traffic")))
                    .also { if(!it.valid())throw IOException("invalid curb snapshot") }
            }
            return CurbSnapshot(session,received)
        } finally { connection.disconnect() }
    }
}
