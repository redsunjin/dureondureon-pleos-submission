package io.swarm.mobility

import android.content.Context
import android.graphics.*
import android.view.View
import android.view.MotionEvent
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeProvider
import java.util.concurrent.Executors
import kotlin.math.*

/** Raster rendering avoids the native navigation application's map/provider and GL pipeline. */
class GpsMapView(context: Context, private val client: PublicMapClient,
    private val onTiles: (Int, Int, Int) -> Unit) : View(context) {
    private val worker = Executors.newFixedThreadPool(2)
    private val bitmaps = object : LinkedHashMap<MapTile,Bitmap>(64,0.75f,true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<MapTile,Bitmap>?) = size > 64
    }
    private val pending = mutableSetOf<MapTile>()
    private val failed = mutableSetOf<MapTile>()
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private var center: GeoPoint? = null
    private var position: GeoPoint? = null
    private var route: RoutePreview? = null
    private var candidates = emptyList<ParkingCandidate>()
    private var selectedId: String? = null
    private var reservedId: String? = null
    var onCandidateSelected: ((String) -> Unit)? = null
    private var zoom = 15
    private var closed = false
    private var lastTileStatus = ""
    init { importantForAccessibility=IMPORTANT_FOR_ACCESSIBILITY_YES; isClickable=true; contentDescription = "앱 지도 · 위치 수신 대기"; setLayerType(LAYER_TYPE_SOFTWARE,null) }

    fun position(point: GeoPoint?) {
        if (point != position) {
            position = point
            if (center == null && point != null) fitCandidates()
            contentDescription = point?.let { "앱 지도 GPS 위도 %.6f 경도 %.6f".format(java.util.Locale.US,it.latitude,it.longitude) }
                ?: "앱 지도 · 현재 위치 없음"
            invalidate()
        }
    }
    fun route(value: RoutePreview?) {
        if (route === value) return
        route = value
        if (value == null) { fitCandidates() } else fit(value.points + listOf(value.origin,value.destination))
        invalidate()
    }
    fun candidates(values: List<ParkingCandidate>, selected: String?, reserved: String?) {
        val valid=values.filter { it.mapPosition?.valid()==true }
        if(candidates==valid && selectedId==selected && reservedId==reserved)return
        val changed = candidates.map { it.spaceId to it.mapPosition } != valid.map { it.spaceId to it.mapPosition }
        candidates=valid;selectedId=selected;reservedId=reserved
        if(changed && route==null) fitCandidates()
        invalidate()
    }
    private fun fitCandidates() { fit(candidates.mapNotNull { it.mapPosition } + listOfNotNull(position)) }
    private fun fit(points: List<GeoPoint>) {
        if(points.isEmpty())return
        for (candidate in 17 downTo 10) {
            val world = points.map { AppMapGeometry.world(it,candidate) }
            val dx = world.maxOf { it.x }-world.minOf { it.x }
            val dy = world.maxOf { it.y }-world.minOf { it.y }
            if (dx < (width-100).coerceAtLeast(1) && dy < (height-100).coerceAtLeast(1)) { zoom = candidate; break }
            zoom = 10
        }
        center = GeoPoint((points.minOf { it.latitude }+points.maxOf { it.latitude })/2,
            (points.minOf { it.longitude }+points.maxOf { it.longitude })/2)
    }
    fun zoomBy(delta: Int) { zoom = (zoom+delta).coerceIn(10,18); failed.clear(); invalidate() }
    fun follow() { center = position; invalidate() }
    fun retry() { failed.clear(); invalidate() }
    override fun onSizeChanged(w: Int,h: Int,oldw: Int,oldh: Int) { route?.let { fit(it.points + listOf(it.origin,it.destination)) } ?: fitCandidates() }
    private fun screen(point: GeoPoint, c: WorldPoint): PointF {
        val p = AppMapGeometry.world(point,zoom)
        val size = 256.0 * (1 shl zoom)
        val dx = (((p.x-c.x+size/2)%size)+size)%size-size/2
        return PointF((width/2+dx).toFloat(),(height/2+p.y-c.y).toFloat())
    }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        canvas.drawColor(Color.rgb(225,231,235))
        val point = center ?: return
        if (closed) return
        val c = AppMapGeometry.world(point,zoom)
        val left = c.x-width/2.0; val top = c.y-height/2.0
        val keys = mutableSetOf<MapTile>()
        for (y in floor(top/256).toInt()..floor((top+height)/256).toInt()) {
            for (x in floor(left/256).toInt()..floor((left+width)/256).toInt()) {
                val key = AppMapGeometry.tile(zoom,x,y) ?: continue
                if (keys.size >= 64 && key !in keys) continue
                keys.add(key)
                val sx = (x*256-left).toFloat(); val sy = (y*256-top).toFloat()
                val bitmap = bitmaps[key]
                if (bitmap != null) canvas.drawBitmap(bitmap,null,RectF(sx,sy,sx+256,sy+256),null)
                else {
                    paint.color=Color.rgb(203,213,221); paint.style=Paint.Style.STROKE; paint.strokeWidth=1f
                    canvas.drawRect(sx,sy,sx+256,sy+256,paint)
                    if (key !in pending && key !in failed) load(key)
                }
            }
        }
        route?.let { preview ->
            paint.color=Color.rgb(16,115,218); paint.style=Paint.Style.STROKE; paint.strokeWidth=7f; paint.pathEffect=null
            val path=Path();preview.points.forEachIndexed { i,p -> val s=screen(p,c);if(i==0)path.moveTo(s.x,s.y) else path.lineTo(s.x,s.y) };canvas.drawPath(path,paint)
            val start=screen(preview.origin,c);val snap=screen(preview.snappedOrigin,c)
            paint.strokeWidth=3f;paint.pathEffect=DashPathEffect(floatArrayOf(8f,7f),0f)
            canvas.drawLine(start.x,start.y,snap.x,snap.y,paint)
            val end=screen(preview.points.last(),c);val dest=screen(preview.destination,c)
            canvas.drawLine(end.x,end.y,dest.x,dest.y,paint);paint.pathEffect=null
            val target=screen(preview.destination,c);paint.style=Paint.Style.FILL;paint.color=Color.rgb(232,107,35)
            canvas.drawCircle(target.x,target.y,10f,paint)
        }
        candidates.forEach { candidate ->
            val p=screen(candidate.mapPosition!!,c)
            val own=candidate.spaceId==reservedId
            paint.style=Paint.Style.FILL
            paint.color=when { own -> Color.rgb(232,107,35); candidate.available -> Color.rgb(24,131,77); else -> Color.DKGRAY }
            val rect=RectF(p.x-64,p.y-42,p.x+64,p.y+8)
            canvas.drawRoundRect(rect,12f,12f,paint)
            if(candidate.spaceId==selectedId) {
                paint.style=Paint.Style.STROKE;paint.strokeWidth=4f;paint.color=Color.WHITE
                canvas.drawRoundRect(rect,12f,12f,paint);paint.style=Paint.Style.FILL
            }
            paint.color=Color.WHITE;paint.textSize=23f;paint.textAlign=Paint.Align.CENTER
            canvas.drawText(candidate.spaceId + if(own) " ★" else "",p.x,p.y-10,paint)
            paint.textAlign=Paint.Align.LEFT
        }
        position?.let {
            val p=screen(it,c);paint.style=Paint.Style.FILL;paint.color=Color.WHITE;canvas.drawCircle(p.x,p.y,12f,paint)
            paint.color=Color.rgb(0,104,216);canvas.drawCircle(p.x,p.y,8f,paint)
        }
        val loaded=keys.count { it in bitmaps };val errors=keys.count { it in failed }
        val status="$loaded/${keys.size}/$errors"
        if (status != lastTileStatus) { lastTileStatus=status; post { if(!closed) onTiles(loaded,keys.size,errors) } }
    }
    private fun pinBounds(index: Int): Rect? {
        val point=candidates.getOrNull(index)?.mapPosition ?: return null
        val c=center?.let { AppMapGeometry.world(it,zoom) } ?: return null
        val p=screen(point,c)
        val rect=Rect((p.x-64).toInt(),(p.y-42).toInt(),(p.x+64).toInt(),(p.y+8).toInt())
        return rect.takeIf { it.intersect(0,0,width,height) }
    }
    private fun pinLabel(candidate: ParkingCandidate) = "${candidate.spaceId} 지도 핀 · " + when {
        candidate.spaceId==reservedId -> "내 예약"
        candidate.available -> "예약 가능"
        candidate.status=="occupied" -> "주차 중"
        else -> "예약 중"
    }
    override fun performClick(): Boolean { super.performClick();return true }
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if(event.action==MotionEvent.ACTION_UP) {
            candidates.indices.firstOrNull { pinBounds(it)?.contains(event.x.toInt(),event.y.toInt())==true }?.let {
                onCandidateSelected?.invoke(candidates[it].spaceId)
            }
            performClick()
        }
        return true
    }
    // Real virtual child nodes let accessibility services and QA select the rendered pins.
    override fun getAccessibilityNodeProvider() = object : AccessibilityNodeProvider() {
        override fun createAccessibilityNodeInfo(id: Int): AccessibilityNodeInfo? {
            if(id==View.NO_ID) return AccessibilityNodeInfo.obtain(this@GpsMapView).also {
                onInitializeAccessibilityNodeInfo(it);it.className="android.view.ViewGroup"
                candidates.indices.filter { index -> pinBounds(index)!=null }.forEach { index -> it.addChild(this@GpsMapView,index) }
            }
            val bounds=pinBounds(id) ?: return null
            return AccessibilityNodeInfo.obtain().apply {
                setSource(this@GpsMapView,id);setParent(this@GpsMapView)
                packageName=context.packageName;className="android.widget.Button"
                text=pinLabel(candidates[id]);contentDescription=text
                isEnabled=true;isClickable=true;isFocusable=true;isVisibleToUser=isShown
                isSelected=candidates[id].spaceId==selectedId
                addAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_CLICK)
                setBoundsInParent(bounds)
                val location=IntArray(2);getLocationOnScreen(location);bounds.offset(location[0],location[1]);setBoundsInScreen(bounds)
            }
        }
        override fun performAction(id: Int, action: Int, arguments: Bundle?): Boolean {
            if(action!=AccessibilityNodeInfo.ACTION_CLICK || candidates.getOrNull(id)==null)return false
            onCandidateSelected?.invoke(candidates[id].spaceId);performClick();return true
        }
    }
    private fun load(key: MapTile) {
        pending.add(key)
        worker.execute {
            val bitmap = runCatching { client.tile(key) }.getOrNull()
            post {
                pending.remove(key)
                if (!closed) {
                    if(bitmap != null) bitmaps[key]=bitmap else failed.add(key)
                    invalidate()
                }
            }
        }
    }
    fun close() { closed=true;worker.shutdownNow();pending.clear();bitmaps.clear() }
}
