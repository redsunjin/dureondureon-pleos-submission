"""Append-only local review notes, bound to one geographic layout.

A note is not geometry verification, occupancy evidence, or permission to guide a car.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import sqlite3
import uuid

from coordination.errors import ApiError
from coordination.site_map import name, timestamp


def note_text(value):
    return isinstance(value,str) and 0<len(value.strip())<=600 and all(ord(c)>=32 or c in '\n\r\t' for c in value)


class SiteReviews:
    def __init__(self, path, site_map, clock):
        self.path,self.site_map,self.clock=path,site_map,clock
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS site_review_notes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    site_id TEXT NOT NULL, map_version INTEGER NOT NULL, geometry_digest TEXT NOT NULL,
                    feature_id TEXT NOT NULL, revision INTEGER NOT NULL, created INTEGER NOT NULL,
                    record TEXT NOT NULL,
                    UNIQUE(site_id,map_version,geometry_digest,feature_id,revision));
                CREATE INDEX IF NOT EXISTS site_review_context ON site_review_notes(site_id,map_version,geometry_digest);
            ''');db.commit()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row;return db

    def site(self):
        site=self.site_map.site
        if site is None:raise ApiError(409,'site_map_unavailable')
        return site

    def snapshot(self,db,site):
        context=(site['siteId'],site['mapVersion'],site['geometryDigest'])
        rows=db.execute('SELECT record FROM site_review_notes WHERE sequence IN (SELECT MAX(sequence) FROM site_review_notes WHERE site_id=? AND map_version=? AND geometry_digest=? GROUP BY feature_id) ORDER BY sequence',
                        context).fetchall()
        latest=[json.loads(r['record']) for r in rows]
        rows=db.execute('SELECT record FROM site_review_notes WHERE site_id=? AND map_version=? AND geometry_digest=? ORDER BY sequence DESC LIMIT 50',
                        (site['siteId'],site['mapVersion'],site['geometryDigest'])).fetchall()
        history=[json.loads(r['record']) for r in reversed(rows)]
        old=db.execute('SELECT COUNT(*) FROM site_review_notes WHERE site_id=? AND NOT (map_version=? AND geometry_digest=?)',
                       (site['siteId'],site['mapVersion'],site['geometryDigest'])).fetchone()[0]
        return {'siteId':site['siteId'],'mapVersion':site['mapVersion'],'geometryDigest':site['geometryDigest'],
                'previewOnly':True,'reviews':latest,'history':history,'previousLayoutNotes':old}

    def state(self):
        site=self.site()
        with closing(self.connect()) as db:return self.snapshot(db,site)

    def save(self,payload):
        site=self.site()
        if (payload.get('siteId')!=site['siteId'] or type(payload.get('mapVersion')) is not int
                or payload['mapVersion']!=site['mapVersion'] or payload.get('geometryDigest')!=site['geometryDigest']):
            raise ApiError(409,'site_review_map_changed')
        fid=payload.get('featureId')
        if not any(f['id']==fid for f in site['features']):raise ApiError(400,'site_review_feature_unknown')
        try:
            request_id=payload['requestId']
            if not isinstance(request_id,str) or str(uuid.UUID(request_id))!=request_id:raise ValueError()
        except (ValueError,KeyError,AttributeError):raise ApiError(400,'invalid_site_review') from None
        revision=payload.get('revision');status=payload.get('status');kind=payload.get('evidenceKind')
        observed=payload.get('observedAt');reviewer=payload.get('reviewer');note=payload.get('note');evidence=payload.get('evidenceRef','')
        if (type(revision) is not int or revision<0 or status not in ('needs_visit','evidence_added','correction_needed')
                or kind not in ('document_review','field_visit') or not timestamp(observed)
                or not name(reviewer,40) or not note_text(note) or not isinstance(evidence,str) or len(evidence)>300
                or evidence and not name(evidence,300) or status=='evidence_added' and not evidence.strip()):
            raise ApiError(400,'invalid_site_review')
        now=self.clock()
        if datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000>now+300_000:
            raise ApiError(400,'site_review_future_time')
        fields={k:payload[k] for k in ('siteId','mapVersion','geometryDigest','featureId','requestId','revision','status','evidenceKind','observedAt','reviewer','note')}
        fields['evidenceRef']=evidence
        fingerprint=hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                receipt=db.execute('SELECT fingerprint FROM site_review_notes WHERE request_id=?',(request_id,)).fetchone()
                if receipt:
                    if receipt['fingerprint']!=fingerprint:raise ApiError(409,'request_id_reused')
                    result=self.snapshot(db,site);db.commit();return result
                latest=db.execute('SELECT MAX(revision) FROM site_review_notes WHERE site_id=? AND map_version=? AND geometry_digest=? AND feature_id=?',
                                  (site['siteId'],site['mapVersion'],site['geometryDigest'],fid)).fetchone()[0] or 0
                if revision!=latest:raise ApiError(409,'site_review_revision_changed')
                record={**fields,'revision':revision+1,'createdAtMillis':now}
                db.execute('INSERT INTO site_review_notes(request_id,fingerprint,site_id,map_version,geometry_digest,feature_id,revision,created,record) VALUES(?,?,?,?,?,?,?,?,?)',
                           (request_id,fingerprint,site['siteId'],site['mapVersion'],site['geometryDigest'],fid,revision+1,now,json.dumps(record,ensure_ascii=False)))
                result=self.snapshot(db,site);db.commit();return result
            except Exception:
                db.rollback();raise
