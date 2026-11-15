"""Validated, read-only geographic review layer; never an occupancy authority."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import re
import hashlib

KINDS = {'access': '도로 접속 후보', 'reference': '위치 참고점', 'zone': '구역', 'row': '주차열', 'space': '주차면', 'aisle': '통로',
         'entrance': '입구', 'exit': '출구', 'closure': '폐쇄 영역'}
GEOMETRIES = {'access': 'Point', 'reference': 'Point', 'zone': 'Polygon', 'row': 'Polygon', 'space': 'Polygon',
              'aisle': 'LineString', 'entrance': 'Point', 'exit': 'Point', 'closure': 'Polygon'}
MAX_BYTES = 1_000_000


def fail():
    # Never echo a submitted file, a coordinate, or a key in diagnostics.
    raise ValueError('invalid_site_map: 현장 지도 파일의 형식·좌표·버전·출처를 확인하세요.')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def name(value, limit=120):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(ord(c) < 32 for c in value)


def timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def source_url(value):
    from urllib.parse import urlsplit
    if not isinstance(value,str) or len(value) > 500:
        return False
    try:
        url = urlsplit(value)
        return url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False


def position(value):
    if not isinstance(value, list) or len(value) != 2 or not all(number(v) for v in value):
        fail()
    lng, lat = value
    if not -180 <= lng <= 180 or not -85 <= lat <= 85:
        fail()
    return value


def _orientation(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _on_segment(a, b, c):
    return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])


def _intersects(a, b, c, d):
    o1, o2, o3, o4 = _orientation(a,b,c), _orientation(a,b,d), _orientation(c,d,a), _orientation(c,d,b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    return any(o == 0 and _on_segment(x,y,p) for o,x,y,p in
               ((o1,a,b,c),(o2,a,b,d),(o3,c,d,a),(o4,c,d,b)))


def ring(value):
    # Single simple rings only in this first adapter; holes must not be silently filled.
    if not isinstance(value, list) or not 4 <= len(value) <= 201 or value[0] != value[-1]:
        fail()
    for p in value:
        position(p)
    if len({tuple(p) for p in value[:-1]}) != len(value)-1:
        fail()
    origin = value[0]
    area = sum(_orientation(origin, a, b) for a,b in zip(value, value[1:]))
    if abs(area) < 1e-14:
        fail()
    segments = list(zip(value, value[1:]))
    for i,(a,b) in enumerate(segments):
        for j in range(i+1, len(segments)):
            if j == i+1 or i == 0 and j == len(segments)-1:
                continue
            if _intersects(a,b,*segments[j]):
                fail()
    return value


def validate_site_map(raw):
    if not isinstance(raw, dict) or raw.get('type') != 'FeatureCollection' or raw.get('coordinateSystem') != 'EPSG:4326':
        fail()
    if not name(raw.get('siteId'), 80) or raw['siteId'] == 'festival-demo' or not name(raw.get('label')):
        fail()
    if type(raw.get('mapVersion')) is not int or raw['mapVersion'] < 1:
        fail()
    if raw.get('usageRights') not in ('operator_supplied', 'licensed') or not name(raw.get('source')) or not timestamp(raw.get('updatedAt')):
        fail()
    if not source_url(raw.get('sourceUrl')) or not name(raw.get('license')):
        fail()
    if raw.get('address') is not None and not name(raw['address'],200):
        fail()
    features = raw.get('features')
    if not isinstance(features, list) or not 1 <= len(features) <= 200:
        fail()
    points, ids, clean = [], set(), []
    for f in features:
        if not isinstance(f, dict) or f.get('type') != 'Feature' or not name(f.get('id'), 80) or f['id'] in ids:
            fail()
        ids.add(f['id'])
        p, g = f.get('properties'), f.get('geometry')
        if not isinstance(p, dict) or not isinstance(g, dict) or p.get('kind') not in KINDS or not name(p.get('label')) or not name(p.get('source')):
            fail()
        if not source_url(p.get('sourceUrl')):
            fail()
        if p.get('sourceUpdatedAt') is not None and not timestamp(p['sourceUpdatedAt']):
            fail()
        if type(p.get('mapVersion')) is not int or p['mapVersion'] != raw['mapVersion'] or g.get('type') != GEOMETRIES[p['kind']]:
            fail()
        accuracy = p.get('accuracyMeters')
        if accuracy is not None and (not number(accuracy) or not 0 <= accuracy <= 1000):
            fail()
        if p.get('verification') not in ('unverified', 'verified'):
            fail()
        if p['verification'] == 'verified' and (accuracy is None or not name(p.get('verifiedBy')) or not timestamp(p.get('verifiedAt'))):
            fail()
        coords = g.get('coordinates')
        if g['type'] == 'Point':
            feature_points = [position(coords)]
        elif g['type'] == 'LineString':
            if not isinstance(coords,list) or not 2 <= len(coords) <= 200:
                fail()
            feature_points = [position(v) for v in coords]
            if len({tuple(v) for v in feature_points}) < 2:
                fail()
        else:
            if not isinstance(coords,list) or len(coords) != 1:
                fail()
            feature_points = ring(coords[0])
        points.extend(feature_points)
        props = {k:p[k] for k in ('kind','label','source','sourceUrl','mapVersion','verification')}
        props['accuracyMeters'] = accuracy
        props['sourceUpdatedAt'] = p.get('sourceUpdatedAt')
        direction=p.get('direction','unknown')
        if direction not in ('forward','backward','both','unknown'):
            fail()
        props['direction']=direction
        if p['verification'] == 'verified':
            props.update(verifiedBy=p['verifiedBy'], verifiedAt=p['verifiedAt'])
        # Do not pass through arbitrary metadata, HTML, SDK URLs, space bindings, or observations.
        clean.append({'type':'Feature','id':f['id'],'properties':props,
                      'geometry':{'type':g['type'],'coordinates':deepcopy(coords)}})
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    if east-west > .1 or north-south > .1:
        fail()
    binding={'siteId':raw['siteId'],'mapVersion':raw['mapVersion'],'features':[
        {'id':f['id'],'geometry':f['geometry'],'kind':f['properties']['kind'],'sourceUrl':f['properties']['sourceUrl'],'direction':f['properties']['direction']} for f in clean]}
    digest=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {k:raw[k] for k in ('type','coordinateSystem','siteId','label','mapVersion','usageRights','source','sourceUrl','license','updatedAt')} | {
        'geometryDigest':digest,
        'address':raw.get('address',''),
        'features':clean, 'bbox':[west,south,east,north], 'previewOnly':True}


class SiteMap:
    def __init__(self, path=None, *, public_key='', provider='osm'):
        if provider not in ('osm','kakao'):
            fail()
        self.provider = provider
        self.site = None
        self.public_key = public_key if isinstance(public_key,str) and re.fullmatch(r'[0-9a-fA-F]{32}',public_key) else ''
        if path:
            file = Path(path)
            if file.stat().st_size > MAX_BYTES:
                fail()
            try:
                raw = json.loads(file.read_text(encoding='utf-8'), parse_constant=lambda _: fail())
                self.site = validate_site_map(raw)
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                fail()

    def public_config(self):
        state = 'missing_site' if self.site is None else 'missing_key' if self.provider == 'kakao' and not self.public_key else 'ready'
        return {'provider':self.provider, 'state':state, 'previewOnly':True,
                'tileTemplate':'https://tile.openstreetmap.org/{z}/{x}/{y}.png' if self.provider == 'osm' else None,
                'site':deepcopy(self.site), 'publicKey':self.public_key if state == 'ready' and self.provider == 'kakao' else None}

    @property
    def enabled(self):
        return self.site is not None and (self.provider == 'osm' or bool(self.public_key))
