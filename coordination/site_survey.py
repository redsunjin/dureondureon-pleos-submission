"""Self-contained, printable field worksheet; never an operational approval."""
from datetime import datetime, timezone
from html import escape


def render_survey(site, reviews, now_ms):
    if (reviews['siteId'], reviews['mapVersion'], reviews['geometryDigest']) != (site['siteId'], site['mapVersion'], site['geometryDigest']):
        raise ValueError('review layout mismatch')
    e=lambda value: escape(str(value), quote=True)
    latest={r['featureId']:r for r in reviews['reviews']}
    sections=[]
    questions={
        'access':'차량 출입 가능 여부 / 입구·출구 구분 / 차단기 / 운영 시간',
        'aisle':'통행 방향 / 현장 표지 / 통로 폭 / 장애물 / 보행 동선 / 임시 폐쇄',
        'zone':'대상 주차장·관리 주체 / 실제 경계 일치 / 측정 방법·위치 오차',
        'reference':'시설 위치 참고점 확인 (차량 입구로 사용하지 않음)',
    }
    statuses={'needs_visit':'현장 확인 필요','evidence_added':'근거 기록','correction_needed':'수정 필요'}
    for i,f in enumerate(site['features'],1):
        p=f['properties'];r=latest.get(f['id']);geometry=f['geometry']
        coords=geometry['coordinates']
        point=coords if geometry['type']=='Point' else coords[0] if geometry['type']=='LineString' else coords[0][0]
        direction={'forward':'좌표 순방향 단방향 표기 · 현장 미확인','backward':'좌표 역방향 단방향 표기 · 현장 미확인','both':'양방향 표기 · 현장 미확인'}.get(p.get('direction'),'미확인')
        previous='<p>현재 배치의 기존 검토 기록 없음</p>'
        if r:
            previous=f"<p><b>기존 기록 (이번 방문 결과와 별도)</b> · {e(statuses[r['status']])} · {e('현장 방문 기록' if r['evidenceKind']=='field_visit' else '자료 검토')} · {e(r['reviewer'])} · 기록 {e(r['revision'])}<br>확인 시점 {e(r['observedAt'])} · 근거 {e(r['evidenceRef'] or '미등록')}</p><p class='note'>{e(r['note'])}</p>"
        sections.append(f"""<section><h2>{i}. {e(p['label'])}</h2><p class='meta'>항목 ID {e(f['id'])}<br>좌표 참고: 위도 {e(point[1])} · 경도 {e(point[0])} (선·경계는 첫 꼭짓점)<br>기하 확인 상태 {e(p['verification'])} · 위치 오차 {e(p['accuracyMeters'] if p['accuracyMeters'] is not None else '미측정')} · 통행 방향 {e(direction)}<br>출처: {e(p['source'])}<br>{e(p['sourceUrl'])}</p>{previous}<p><b>현장 확인 항목</b> {e(questions.get(p['kind'],'위치·용도·통행 조건 확인'))}</p><p>확인 시점(시간대 포함): ____________________　조사자: ____________________</p><p>확인 방식: □ 현장 방문　□ 자료 검토　　결과: □ 현장 확인 필요　□ 근거 기록　□ 수정 필요</p><p>근거 주소·사진/기록 번호: __________________________________________________</p><div class='writing'>확인 사실·측정 방법·오차·추가 확인 내용:</div><p>□ 화면에 옮겨 기록함 (자동 반영되지 않음)</p></section>""")
    generated=datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat()
    return f"""<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>현장 조사표 · {e(site['label'])}</title><style>
body{{font:15px/1.6 sans-serif;max-width:850px;margin:32px auto;padding:0 20px;color:#172f32}}h1{{font-size:26px}}h2{{font-size:20px}}section{{border-top:2px solid #456;margin-top:28px;padding-top:12px;break-inside:avoid}}.meta{{font-size:12px;overflow-wrap:anywhere}}.note{{white-space:pre-wrap}}.writing{{height:100px;border:1px solid #aaa;padding:8px}}.warning{{border:2px solid #98651a;padding:12px}}@page{{size:A4;margin:15mm}}@media print{{body{{margin:0;padding:0;font-size:11pt}}section{{break-before:page}}h1{{font-size:20pt}}}}
</style><h1>현장 조사표</h1><h2>{e(site['label'])}</h2><p>{e(site.get('address',''))}</p><p class='warning'>현장 확인 전 준비 자료 · 빈자리 안내·차량 이동 승인용 아님<br>도로 접속 후보는 입출구 미확인입니다. 자료의 통행 방향을 현장 표지와 대조하세요.</p><p>조사 일자: ____________________　조사자: ____________________<br>관리 주체·조사 범위 확인: __________________________________________</p><p>브라우저의 인쇄 기능으로 인쇄하거나 PDF로 저장할 수 있습니다. 항목마다 한 페이지로 나뉩니다. 작성한 종이는 현재 배치를 확인한 뒤 지도 화면에 직접 기록하세요.</p><p class='meta'>현장 ID {e(site['siteId'])} · 배치 {e(site['mapVersion'])}판 · {len(site['features'])}개 항목<br>배치 식별값 {e(site['geometryDigest'])}<br>자료 조회일 {e(site['updatedAt'])} · 내보낸 시점(UTC) {e(generated)}<br>라이선스 {e(site['license'])} · {e(site['sourceUrl'])}<br>이전 배치 기록 {e(reviews['previousLayoutNotes'])}건은 포함하지 않습니다. 현재 배치의 항목별 최신 메모만 포함합니다. 검증용 QA 메모도 실제 현장 근거와 구분하세요.</p>{''.join(sections)}</html>"""
