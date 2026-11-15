package io.swarm.mobility

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.WindowInsets
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.Executors
import kotlin.math.abs

/** Debug role console: all consent buttons explicitly stand in for simulated peers. */
class CurbActivity: Activity() {
    private val handler=Handler(Looper.getMainLooper())
    private val worker=Executors.newSingleThreadExecutor()
    private val client=CurbClient(BuildConfig.COORDINATION_BASE_URL)
    private val preferences by lazy { getSharedPreferences("curb-session",MODE_PRIVATE) }
    private var snapshot: CurbSnapshot?=null
    private var connected=false
    private var busy=false
    private var visible=false
    private var generation=0
    private var error: String?=null
    private var actionNotice: String?=null
    private lateinit var notice: TextView
    private lateinit var planContext: TextView
    private lateinit var replan: Button
    private lateinit var connection: TextView
    private lateinit var status: TextView
    private lateinit var progress: TextView
    private lateinit var board: CurbRoadView
    private lateinit var create: Button
    private lateinit var start: Button
    private lateinit var stop: Button
    private lateinit var refresh: Button
    private val observationButtons=mutableListOf<Button>()
    private val trafficButtons=mutableListOf<Button>()
    private lateinit var trafficInfo: TextView
    private lateinit var container: LinearLayout
    private var cardsSession: String?=null
    private val cards=mutableMapOf<String,Card>()
    private data class Card(val state: TextView,val progress: ProgressBar,val approve: Button,val reject: Button,val withdraw: Button)
    private val poller=object: Runnable {
        override fun run() { if(visible) { if(!busy)fetch();handler.postDelayed(this,500) } }
    }
    private val ticker=object: Runnable {
        override fun run() { if(visible) { render();handler.postDelayed(this,200) } }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if(!BuildConfig.CURB_DEMO_ENABLED) { finish();return }
        val scroll=ScrollView(this).apply { id=R.id.curb_scroll;setBackgroundColor(getColor(R.color.swarm_background)) }
        scroll.setOnApplyWindowInsetsListener { view,insets ->
            if(Build.VERSION.SDK_INT>=30) {
                val safe=insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                view.setPadding(safe.left,safe.top,safe.right,safe.bottom)
            } else {
                @Suppress("DEPRECATION")
                view.setPadding(insets.systemWindowInsetLeft,insets.systemWindowInsetTop,insets.systemWindowInsetRight,insets.systemWindowInsetBottom)
            };insets
        }
        val body=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL;setPadding(dp(20),dp(12),dp(20),dp(24)) }
        scroll.addView(body)
        body.addView(text("협력 평행주차 · 차량별 요청과 동의",24f))
        body.addView(text("가상 차량 5대 · 각 차량의 응답을 대신 입력하는 테스트 화면입니다.",14f))
        body.addView(text("실제 차량 제어 없음 · 관제의 시뮬레이션 상태를 4배속으로 표시",14f))
        connection=text("관제 연결 확인 중",15f);status=text("계획 확인 대기",17f);progress=text("",15f)
        notice=text("",14f);planContext=text("",14f)
        body.addView(connection);body.addView(status);body.addView(notice);body.addView(planContext);body.addView(progress)
        trafficInfo=text("이동 장애물 관측 미설정",14f);body.addView(trafficInfo)
        val actions=LinearLayout(this)
        create=button("초기 배치로 새 계획") { act("create") }
        replan=button("현재 배치에서 재계획") { act("replan") }
        listOf(replan,create).forEach { actions.addView(it,LinearLayout.LayoutParams(0,-2,1f)) };body.addView(actions)
        val runActions=LinearLayout(this)
        start=button("주차 시뮬레이션 시작") { act("start") }
        stop=button("모든 이동 중단") { act("stop") }
        listOf(start,stop).forEach { runActions.addView(it,LinearLayout.LayoutParams(0,-2,1f)) };body.addView(runActions)
        body.addView(text("‘초기 배치로 새 계획’은 처음으로 되돌립니다. 재계획은 현재 위치를 유지합니다.",13f))
        board=CurbRoadView(this);body.addView(board,LinearLayout.LayoutParams(-1,dp(205)))
        body.addView(text("가상 변화 시험 · 장애물 (16.0, 5.9)m / 크기 0.25m · NEW는 마지막 수신 위치에서 y +0.15m",13f))
        val observationActions=LinearLayout(this)
        listOf("장애물 추가(시험)" to "obstacle","NEW 위치 변경(시험)" to "vehicle","장애물 제거(시험)" to "clear").forEach { (label,kind)->
            val b=button(label) { act("observe",kind) };observationButtons.add(b)
            observationActions.addView(b,LinearLayout.LayoutParams(0,-2,1f))
        };body.addView(observationActions)
        body.addView(text("이동 시험 · 보행자/차량은 정해진 가상 궤적으로 통과 · 위험 해소 후에도 재계획·재동의 필요",13f))
        val trafficActions=LinearLayout(this)
        listOf("보행자 횡단(시험)" to "pedestrian","이동 차량 접근(시험)" to "vehicle","이동 관측 비우기(시험)" to "clear").forEach { (label,kind)->
            val b=button(label) { act("traffic",kind) };trafficButtons.add(b)
            trafficActions.addView(b,LinearLayout.LayoutParams(0,-2,1f))
        };body.addView(trafficActions)
        body.addView(text("차량별 요청 · 공간 확보 후 주차·간격 재정렬까지 포함",16f))
        container=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL };body.addView(container)
        refresh=button("협력 상태 다시 확인") { fetch() };body.addView(refresh)
        body.addView(button("기존 화면으로") { finish() })
        setContentView(scroll);scroll.requestApplyInsets()
    }
    override fun onStart() {
        super.onStart();if(!BuildConfig.CURB_DEMO_ENABLED)return
        visible=true;generation++;busy=false;connected=false;poller.run();ticker.run()
    }
    override fun onStop() {
        visible=false;generation++;handler.removeCallbacks(poller);handler.removeCallbacks(ticker);super.onStop()
        // No hidden resume: after heartbeat loss the server freezes its last persisted frame.
    }
    override fun onDestroy() { worker.shutdownNow();super.onDestroy() }
    private fun text(value: String,size: Float)=TextView(this).apply { text=value;textSize=size;setTextColor(getColor(R.color.swarm_text)) }
    private fun button(value: String, action: ()->Unit)=Button(this).apply { text=value;contentDescription=value;textSize=14f;setOnClickListener { action() } }
    private fun dp(v: Int)=(v*resources.displayMetrics.density).toInt()
    private fun fetch() { request(null) }
    private fun act(action: String,id: String?=null) {
        val now=SystemClock.elapsedRealtime()
        if(!CurbUiPolicy.allowed(snapshot,connected,preferences.contains("pending"),now,action,id))return
        actionNotice=null
        val s=snapshot?.session
        val payload=if(action=="traffic" && s!=null && id!=null)CurbClient.traffic(s,id)
            else if(action=="observe" && s!=null && id!=null)CurbClient.observation(s,id)
            else CurbClient.payload(action,s,s?.vehicles?.find { it.id==id })
        if(!preferences.edit().putString("pending",payload.toString()).commit()) {
            error="요청을 저장하지 못했습니다 · 상태를 다시 확인해 주세요.";render();return
        }
        request(payload);render()
    }
    private fun request(payload: JSONObject?) {
        if(!visible || busy)return
        busy=true;val token=generation
        worker.execute {
            var action=payload
            val result=runCatching {
                if(action==null)action=preferences.getString("pending",null)?.let { JSONObject(it) }
                (action?.let { client.submit(it) } ?: client.state())
            }
            handler.post {
                if(!visible || token!=generation)return@post
                busy=false
                result.onSuccess {
                    if(action!=null)preferences.edit().remove("pending").commit()
                    snapshot=it;connected=true;error=null
                    if(BuildConfig.DEBUG)Log.i("SwarmCurb","state=${it.session?.status} stage=${it.session?.stage} actor=${it.session?.actor} elapsed=${it.session?.elapsed} revision=${it.session?.revision}")
                }.onFailure {
                    connected=false
                    if(it is ApiFailure && it.status in 400..499)preferences.edit().remove("pending").commit()
                    if(it is ApiFailure && it.code=="curb_replan_unavailable")
                        actionNotice="현재 장애물·차량 배치에서 경로를 찾지 못했습니다. 변경된 위치에서 중단을 유지합니다."
                    if(it is ApiFailure && it.code=="curb_traffic_unsafe")
                        actionNotice="이동 장애물 위험 또는 관측 지연으로 요청을 보류했습니다. 새 상태를 확인한 뒤 재계획해 주세요."
                    error=when {
                        it is ApiFailure && it.code=="curb_replan_unavailable" -> "재계획 불가 · 현재 위치 유지"
                        preferences.contains("pending") -> "요청 결과 확인 대기 · 같은 요청으로 다시 확인합니다."
                        it is ApiFailure && it.status==409 -> "계획 상태가 바뀌었습니다 · 최신 상태를 다시 확인합니다."
                        else -> "관제 연결 끊김 · 마지막 수신 화면 유지 · 이동 중단 확인 필요"
                    }
                };render()
            }
        };render()
    }
    private fun rebuild(s: CurbSession?) {
        if(cardsSession==s?.id && (s!=null || cards.isEmpty()))return
        cardsSession=s?.id;cards.clear();container.removeAllViews()
        s?.vehicles?.forEach { v ->
            val box=LinearLayout(this).apply { orientation=LinearLayout.VERTICAL;setPadding(dp(12),dp(5),dp(12),dp(5));setBackgroundColor(getColor(R.color.swarm_surface)) }
            box.addView(text("${v.id} · ${v.role}",17f))
            val steps=v.steps.filter { abs(it.distance)>0.001 }.joinToString(" → ") {
                val direction=if(it.distance>=0)"앞으로" else "뒤로"
                String.format(Locale.US,"%s %s %.2fm",CurbLabels.stage(it.stage),direction,abs(it.distance))
            }.ifEmpty { "추가 이동 없음 · 현재 위치 유지에 재동의" }
            box.addView(text("이동 요청: $steps",14f))
            val state=text("동의 대기",15f);box.addView(state)
            val gauge=ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply { max=100 };box.addView(gauge,LinearLayout.LayoutParams(-1,dp(6)))
            val buttons=LinearLayout(this)
            val approve=button("${v.id} ${if(s.replanGeneration>0) "재동의" else "동의"}") { act("approve",v.id) }
            val reject=button("${v.id} 거부") { act("reject",v.id) }
            val withdraw=button("${v.id} 철회") { act("withdraw",v.id) }
            listOf(approve,reject,withdraw).forEach { buttons.addView(it,LinearLayout.LayoutParams(0,-2,1f)) };box.addView(buttons)
            container.addView(box,LinearLayout.LayoutParams(-1,-2).apply { bottomMargin=dp(6) })
            cards[v.id]=Card(state,gauge,approve,reject,withdraw)
        }
    }
    private fun TextView.display(value: String) { if(text.toString()!=value)text=value }
    private fun render() {
        if(!::status.isInitialized)return
        val now=SystemClock.elapsedRealtime();val s=snapshot?.session
        val fresh=CurbUiPolicy.fresh(snapshot,connected,now)
        val locked=preferences.contains("pending")
        connection.display(when { !connected -> error ?: "관제 연결 확인 중"; !fresh -> "상태 수신 지연 · 마지막 화면 유지";else -> "관제 연결됨 · 가상 차량 상태 수신" })
        status.display(if(s!=null && !fresh) "새 상태 수신 대기 · 마지막 위치 유지 · 이동 중단 확인 필요" else s?.let { CurbLabels.reason(it.reason) } ?: "새 계획을 만들면 차량별 이동 요청이 표시됩니다.")
        if(locked && preferences.getString("pending",null)?.let { runCatching { JSONObject(it).optString("action") }.getOrNull() }=="replan")
            status.display("현재 배치에서 경로 재계산 중 · 차량 위치 유지")
        notice.visibility=if(actionNotice==null)View.GONE else View.VISIBLE
        notice.display(actionNotice.orEmpty())
        planContext.display((if((s?.replanGeneration ?: 0)>0) "현재 배치 재계획 ${s!!.replanGeneration}회 · 이전 동의 사용 안 함 · 진행률은 남은 이동 기준" else "초기 배치 계획")+
            (s?.let { " · 환경 변경 ${it.environmentRevision}회 · 장애물 ${it.obstacles.size}개 · 점선은 새 차량 계획 경로" } ?: ""))
        val approved=s?.vehicles?.count { it.decision=="approved" } ?: 0
        trafficInfo.display(s?.traffic?.let { t->
            val safety=when(t.safety) {
                "risk"->"충돌 위험 예상 · ${t.riskId ?: "이동 장애물"}"+(t.timeToRisk?.let { String.format(Locale.US," · 가상 %.1f초 이내",it) } ?: "")+" · 중단 유지"
                "stale"->"이동 관측 오래됨 · 안전 판단 보류 · 새 관측 필요"
                "clearing"->String.format(Locale.US,"위험 해소 확인 중 · %.1f초 후 재확인",t.clearRemainingMillis/1000.0)
                "clear"->"예측 구간 위험 없음 · 중단 후에는 재계획·재동의 필요"
                else->"이동 장애물 관측 미설정"
            }
            safety+(if(t.source=="scripted")" · 가상 시험 궤적" else if(t.source=="observed")" · 입력 관측" else "")+
                (if(t.tracks.isNotEmpty())"\n"+t.tracks.joinToString(" / ") { o->String.format(Locale.US,"%s x %.1f y %.1f · 속도 (%.1f, %.1f)m/s",if(o.kind=="pedestrian")"보행자" else "이동 차량",o.pose.x,o.pose.y,o.vx,o.vy) } else "")
        } ?: "이동 장애물 관측 미설정")
        progress.display(s?.let {
            val geometry=if(it.replanGeneration>0)String.format(Locale.US,"현재 주차 구간 %.2fm",it.initialGap)
                else String.format(Locale.US,"확보 %.2f→%.2fm",it.initialGap,it.openingGap)
            String.format(Locale.US,"%s %d/%d · %s · 이동 %s · 가상 %.1f/%.1f초 · %s",
                if(it.replanGeneration>0)"재동의" else "동의",approved,it.vehicles.size,CurbLabels.stage(it.stage),
                it.actor ?: "없음",it.elapsed,it.duration,geometry) +
                (it.completedGaps()?.let { g->String.format(Locale.US," · 완료 간격 뒤 %.2fm / 앞 %.2fm",g.first,g.second) } ?: "")
        } ?: "")
        create.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"create")
        replan.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"replan")
        start.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"start")
        stop.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"stop")
        refresh.isEnabled=!locked
        observationButtons.forEach { it.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"observe") }
        trafficButtons.forEach { it.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"traffic") }
        rebuild(s);board.show(s,fresh)
        s?.vehicles?.forEach { v ->
            val card=cards[v.id] ?: return@forEach
            val decision=when(v.decision) { "approved"->if(s.replanGeneration>0)"재동의 완료" else "동의 완료";"rejected"->"거부";"withdrawn"->"동의 철회";else->if(s.replanGeneration>0)"재동의 대기" else "동의 대기" }
            val motion=if(v.totalTravel<=0.000001 && s.status=="completed")"위치 유지 완료" else when(v.motion) { "moving"->"이동 중";"steering"->"정지 조향 중";"complete"->"이동 완료";"stopped"->"중단";"holding"->"현재 위치 유지";else->"순서 대기" }
            card.progress.visibility=if(v.totalTravel<=0.000001)View.GONE else View.VISIBLE
            card.state.display(if(v.totalTravel<=0.000001 && s.status!="completed") "$decision · 추가 이동 없음 · ${if(fresh) "현재 위치 유지" else "마지막 수신 상태"}" else String.format(Locale.US,"%s · %s · 진행 %.0f%%",decision,if(fresh)motion else "마지막 수신 상태",v.progress))
            card.progress.progress=v.progress.toInt()
            card.approve.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"approve",v.id)
            card.reject.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"reject",v.id)
            card.withdraw.isEnabled=CurbUiPolicy.allowed(snapshot,connected,locked,now,"withdraw",v.id)
        }
    }
}
