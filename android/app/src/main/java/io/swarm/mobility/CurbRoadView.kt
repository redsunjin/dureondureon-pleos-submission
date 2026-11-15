package io.swarm.mobility

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.DashPathEffect
import android.graphics.Path
import android.view.View
import java.util.Locale
import kotlin.math.min

/** The road and every car pose come from the server's simulator frame, never GPS. */
class CurbRoadView(context: Context): View(context) {
    private val paint=Paint(Paint.ANTI_ALIAS_FLAG)
    private var session: CurbSession?=null
    init { setLayerType(LAYER_TYPE_SOFTWARE,null);contentDescription="협력 주차 모형 · 계획 대기" }
    private var fresh=false
    fun show(value: CurbSession?, isFresh: Boolean) {
        if(session?.vehicles==value?.vehicles && session?.obstacles==value?.obstacles && session?.traffic==value?.traffic && session?.plannedPath==value?.plannedPath && session?.stage==value?.stage && session?.actor==value?.actor && fresh==isFresh)return
        session=value;fresh=isFresh
        contentDescription=value?.let { s->
            "${if(isFresh) "협력 주차 모형" else "마지막 수신 모형"} · ${CurbLabels.stage(s.stage)} · 이동 ${s.actor ?: "없음"} · "+
                (s.vehicles.find { it.id=="NEW" }?.pose?.let { p->String.format(Locale.US,"새 차량 x %.2f y %.2f 방향 %.3f",p.x,p.y,p.yaw) } ?: "")+" · 장애물 ${s.obstacles.size}개 · 이동 장애물 ${s.traffic.tracks.size}개 · 계획 경로 ${s.plannedPath.size}점"
        } ?: "협력 주차 모형 · 계획 대기"
        invalidate()
    }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas);canvas.drawColor(Color.rgb(27,43,53))
        val s=session ?: return
        val scale=min((width-32f)/s.roadLength.toFloat(),(height-34f)/s.roadWidth.toFloat())
        val ox=(width-s.roadLength*scale)/2;val oy=(height+s.roadWidth*scale)/2-7
        fun x(v: Double)=(ox+v*scale).toFloat()
        fun y(v: Double)=(oy-v*scale).toFloat()
        paint.style=Paint.Style.FILL;paint.color=Color.rgb(32,65,64)
        canvas.drawRect(x(0.0),y(2.8),x(s.roadLength),y(0.0),paint)
        paint.color=Color.rgb(160,190,198);paint.strokeWidth=2f
        canvas.drawLine(x(0.0),y(0.0),x(s.roadLength),y(0.0),paint)
        paint.textSize=12f;canvas.drawText("연석 · 진행 방향 →",x(0.0)+4,y(0.0)+16,paint)
        if(s.plannedPath.size>1) {
            val path=Path();s.plannedPath.forEachIndexed { i,p->if(i==0)path.moveTo(x(p.x),y(p.y)) else path.lineTo(x(p.x),y(p.y)) }
            paint.style=Paint.Style.STROKE;paint.color=Color.rgb(250,208,107);paint.strokeWidth=2f;paint.pathEffect=DashPathEffect(floatArrayOf(7f,5f),0f)
            canvas.drawPath(path,paint);paint.pathEffect=null;paint.style=Paint.Style.FILL
        }
        for(o in s.obstacles) {
            canvas.save();canvas.translate(x(o.pose.x),y(o.pose.y));canvas.rotate((-Math.toDegrees(o.pose.yaw)).toFloat())
            paint.color=Color.rgb(242,120,101)
            canvas.drawRect((-o.length*scale/2).toFloat(),(-o.width*scale/2).toFloat(),(o.length*scale/2).toFloat(),(o.width*scale/2).toFloat(),paint)
            paint.textSize=12f;canvas.drawText("장애물",4f,-8f,paint);canvas.restore()
        }
        for(o in s.traffic.tracks) {
            paint.color=if(s.traffic.safety=="risk")Color.rgb(255,126,106) else Color.rgb(242,198,98)
            paint.strokeWidth=2f
            canvas.drawLine(x(o.pose.x),y(o.pose.y),x(o.predictedPose.x),y(o.predictedPose.y),paint)
            canvas.save();canvas.translate(x(o.pose.x),y(o.pose.y));canvas.rotate((-Math.toDegrees(o.pose.yaw)).toFloat())
            val l=(o.length*scale).toFloat();val w=(o.width*scale).toFloat()
            if(o.kind=="pedestrian")canvas.drawCircle(0f,0f,kotlin.math.max(l,w)/2,paint)
            else canvas.drawRoundRect(RectF(-l/2,-w/2,l/2,w/2),3f,3f,paint)
            canvas.restore();paint.textSize=12f
            canvas.drawText(if(o.kind=="pedestrian")"보행자 → 예상" else "이동 차량 → 예상",x(o.pose.x)+5,y(o.pose.y)-8,paint)
        }
        for(c in s.vehicles) {
            canvas.save();canvas.translate(x(c.pose.x),y(c.pose.y));canvas.rotate((-Math.toDegrees(c.pose.yaw)).toFloat())
            paint.color=when { c.decision in setOf("rejected","withdrawn") -> Color.rgb(235,139,119)
                c.id=="NEW" -> Color.rgb(98,184,252);c.decision=="approved" -> Color.rgb(118,217,179)
                else -> Color.rgb(149,164,173) }
            val l=(c.length*scale).toFloat();val w=(c.width*scale).toFloat()
            canvas.drawRoundRect(RectF(-l/2,-w/2,l/2,w/2),5f,5f,paint)
            paint.color=Color.rgb(20,43,54);canvas.drawRect(-l*.15f,-w*.35f,l*.23f,w*.35f,paint)
            paint.textSize=12f;paint.textAlign=Paint.Align.CENTER
            canvas.drawText(if(c.id=="NEW")"내 차" else c.id,-l*.32f,4f,paint)
            paint.color=Color.WHITE;canvas.drawRect(l*.41f,-w*.3f,l*.43f,w*.3f,paint)
            paint.textAlign=Paint.Align.LEFT;canvas.restore()
        }
    }
}
