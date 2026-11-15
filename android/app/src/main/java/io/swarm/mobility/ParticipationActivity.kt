package io.swarm.mobility

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.WindowInsets
import android.os.Build
import android.widget.*
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.Executors

/** One invitation, one virtual vehicle. No vehicle actuation or automatic consent. */
class ParticipationActivity: Activity() {
    private val handler=Handler(Looper.getMainLooper())
    private val worker=Executors.newSingleThreadExecutor()
    private val client=ParticipationClient(BuildConfig.COORDINATION_BASE_URL)
    private val prefs by lazy { getSharedPreferences("participation",MODE_PRIVATE) }
    private lateinit var input: EditText
    private lateinit var connect: Button
    private lateinit var info: TextView
    private lateinit var notice: TextView
    private lateinit var retry: Button
    private val actions=mutableMapOf<String,Button>()
    private var token=""
    private var state: JSONObject?=null
    private var pending: JSONObject?=null
    private var received=0L
    private var online=false
    private var visible=false
    private var busy=false
    private var queued=false
    private var message="참여 링크를 입력하고 요청을 확인하세요."
    private val poller=object: Runnable {
        override fun run() {
            if(!visible)return
            render()
            if(!busy && token.isNotEmpty())request(null)
            handler.postDelayed(this,800)
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if(!BuildConfig.CURB_DEMO_ENABLED){finish();return}
        token=prefs.getString("token","") ?: ""
        pending=try { prefs.getString("pending",null)?.let { JSONObject(it) } }catch(_: Exception){null}
        val body=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL;setPadding(dp(24),dp(24),dp(24),dp(24)) }
        val scroll=ScrollView(this).apply { addView(body) }
        scroll.setOnApplyWindowInsetsListener { v,ins ->
            if(Build.VERSION.SDK_INT>=30){val safe=ins.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout());v.setPadding(safe.left,safe.top,safe.right,safe.bottom)}
            ins
        }
        fun text(t: String,size: Float=22f)=TextView(this).apply { text=t;textSize=size;setPadding(0,12,0,12) }.also { body.addView(it) }
        text("내 차량 협력 주차 참여",30f)
        text("가상 차량 응답 시험 · 실제 차량 이동 없음\n관제에서 받은 차량별 링크로 자신의 계획에만 응답합니다.")
        input=EditText(this).apply { hint="차량 참여 링크 또는 코드";textSize=20f;minHeight=dp(60);inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD;setSingleLine(true);setText(token);setSelectAllOnFocus(true);transformationMethod=android.text.method.PasswordTransformationMethod.getInstance() };body.addView(input)
        connect=Button(this).apply { textSize=20f;minHeight=dp(60);text="내 이동 요청 확인";setOnClickListener {
            val value=ParticipationPolicy.token(input.text.toString())
            if(value==null){message="올바른 참여 링크를 입력하세요.";render()}
            else if(!busy && pending==null){token=value;state=null;online=false;prefs.edit().putString("token",token).commit();request(null)}
        } };body.addView(connect)
        notice=text("");info=text("")
        for((action,label) in listOf("approve" to "이 이동 계획에 동의","reject" to "참여 거부","withdraw" to "동의 철회·중단")) {
            val b=Button(this).apply { textSize=20f;minHeight=dp(60);text=label;setOnClickListener { respond(action) } };actions[action]=b;body.addView(b)
        }
        retry=Button(this).apply { textSize=20f;minHeight=dp(60);text="같은 요청으로 결과 다시 확인";setOnClickListener { pending?.let { if(busy){queued=true;message="응답 전송 대기";render()}else request(it) } } };body.addView(retry)
        text("전원 동의 후 관제에서 시작합니다. 화면을 닫거나 응답이 끊기면 중단될 수 있습니다. 재계획 뒤에는 새 링크를 받아 재동의하세요.")
        setContentView(scroll);render()
    }
    override fun onResume(){super.onResume();visible=true;handler.post(poller)}
    override fun onPause(){visible=false;online=false;queued=false;handler.removeCallbacks(poller);super.onPause()}
    override fun onDestroy(){worker.shutdownNow();super.onDestroy()}
    private fun request(body: JSONObject?) {
        if(busy || token.isEmpty())return
        busy=true;if(body!=null)render()
        val boundToken=token
        val started=SystemClock.elapsedRealtime()
        worker.execute {
            var result: JSONObject?=null;var failure: Exception?=null
            try { result=client.call(if(body==null)"poll" else "respond",body ?: JSONObject().put("token",boundToken)) }catch(e: Exception){failure=e}
            handler.post {
                busy=false
                if(result!=null){state=result;received=started;online=visible
                    if(body!=null){pending=null;prefs.edit().remove("pending").commit();message="응답 결과를 확인했습니다."}
                    else if(pending==null)message="관제 연결됨"
                }else{online=false;message="연결 확인 실패 · 추가 응답을 잠급니다."
                    val error=failure
                    if(body!=null && error is ApiFailure && error.status in 400..499){pending=null;prefs.edit().remove("pending").commit();message="요청이 반영되지 않았습니다. 최신 요청 확인 후 다시 응답하세요."}
                    else if(body!=null)message="저장 결과 확인 대기 · 같은 요청으로 재확인하세요."
                }
                if(!isDestroyed)render()
                if(queued && visible && pending!=null){queued=false;request(pending)}
            }
        }
    }
    private fun respond(action: String) {
        val s=state?.optJSONObject("session") ?: return
        val p=state?.optJSONObject("participant") ?: return
        if(!online || !ParticipationPolicy.allowed(action,s.getString("status"),p.getString("decision"),SystemClock.elapsedRealtime()-received,pending!=null))return
        val body=JSONObject().put("token",token).put("requestId",UUID.randomUUID().toString()).put("revision",s.getInt("revision"))
            .put("action",action).put("moveRequestId",p.getString("moveRequestId"))
        if(!prefs.edit().putString("pending",body.toString()).commit()){message="요청 보관 실패 · 전송하지 않았습니다.";render();return}
        pending=body
        if(busy){queued=true;message="응답 전송 대기";render()}else request(body)
    }
    private fun dp(value: Int)=(value*resources.displayMetrics.density).toInt()
    private fun render() {
        if(!::info.isInitialized)return
        if(notice.text.toString()!=message)notice.text=message
        input.isEnabled=!busy && pending==null;connect.isEnabled=!busy && pending==null
        val s=state?.optJSONObject("session");val p=state?.optJSONObject("participant")
        val status=mapOf("awaiting" to "동의 대기","ready" to "전원 동의 · 시작 대기","running" to "가상 이동 진행","stopped" to "중단","rejected" to "거부로 종료","expired" to "만료","completed" to "완료")
        val display=if(s==null || p==null)"아직 받은 이동 요청이 없습니다." else buildString {
            append("${p.getString("vehicleId")} · ${p.getString("role")}\n${status[s.getString("status")]}\n")
            val reason=mapOf("participant_connection_lost" to "참여 차량 응답 끊김","connection_lost" to "관제 응답 끊김","consent_withdrawn" to "동의 철회","plan_expired" to "계획 만료","server_restarted" to "관제 재시작","vehicle_rejected" to "참여 거부")
            reason[s.getString("reason")]?.let { append("$it\n") }
            append("내 응답 ${mapOf("pending" to "대기","approved" to "동의","rejected" to "거부","withdrawn" to "철회")[p.getString("decision")]} · 전원 ${state!!.getInt("approvedCount")}/${state!!.getInt("participantCount")} 동의\n")
            append("예정 총 이동 ${"%.2f".format(p.getDouble("totalTravel"))}m\n")
            val steps=p.getJSONArray("steps")
            for(i in 0 until steps.length()){val step=steps.getJSONObject(i);val distance=step.getDouble("distance");append("${if(distance<0)"후진" else if(distance>0)"전진" else "정지 중 조향"} ${"%.2f".format(kotlin.math.abs(distance))}m\n")}
            append("계획 ${s.getString("sessionId")}\n응답 버전 ${s.getInt("revision")}")
        }
        if(info.text.toString()!=display)info.text=display
        actions.forEach { (action,b)->b.isEnabled=online && s!=null && p!=null && ParticipationPolicy.allowed(action,s.getString("status"),p.getString("decision"),SystemClock.elapsedRealtime()-received,pending!=null) }
        retry.visibility=if(pending==null)android.view.View.GONE else android.view.View.VISIBLE;retry.isEnabled=!busy
    }
}
