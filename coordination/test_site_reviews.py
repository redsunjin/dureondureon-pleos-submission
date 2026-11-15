from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from coordination.errors import ApiError
from coordination.site_map import SiteMap,validate_site_map
from coordination.site_reviews import SiteReviews
from coordination.vacancy import VacancyLedger

DATA=Path(__file__).parent/'data'/'sejong-arboretum.geojson'


class SiteReviewTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=Path(self.folder.name)/'review.db'
        self.now=int(datetime.fromisoformat('2026-10-31T12:00:00+09:00').timestamp()*1000)
        self.model=SiteMap(DATA);self.store=SiteReviews(self.path,self.model,lambda:self.now)

    def tearDown(self):self.folder.cleanup()

    def payload(self,**kw):
        site=self.model.site
        return dict(siteId=site['siteId'],mapVersion=site['mapVersion'],geometryDigest=site['geometryDigest'],featureId=site['features'][0]['id'],
                    requestId=str(uuid.uuid4()),revision=0,status='needs_visit',reviewer='QA-01',evidenceKind='document_review',
                    observedAt='2026-10-31T10:00:00+09:00',evidenceRef='https://www.openstreetmap.org/way/1113205038',note='시험용 자료 검토\n현장 확인은 하지 않음.',**kw)

    def test_record_persists_on_reopen_and_does_not_verify_geometry(self):
        data=self.store.save(self.payload());self.assertEqual(data['reviews'][0]['revision'],1)
        restored=SiteReviews(self.path,self.model,lambda:self.now).state()
        self.assertEqual(restored['reviews'],data['reviews']);self.assertTrue(restored['previewOnly'])
        self.assertEqual(self.model.site['features'][0]['properties']['verification'],'unverified')

    def test_response_loss_retry_does_not_append_duplicate(self):
        p=self.payload();first=self.store.save(p);again=self.store.save(p)
        self.assertEqual(first,again);self.assertEqual(len(again['history']),1)

    def test_reused_uuid_with_changed_content_is_rejected(self):
        p=self.payload();self.store.save(p);p['note']='다른 내용'
        with self.assertRaises(ApiError) as e:self.store.save(p)
        self.assertEqual(e.exception.code,'request_id_reused')

    def test_stale_revision_does_not_overwrite_another_review(self):
        p=self.payload();self.store.save(p)
        with self.assertRaises(ApiError) as e:self.store.save(self.payload())
        self.assertEqual(e.exception.code,'site_review_revision_changed')
        p=self.payload();p.update(revision=1,status='correction_needed');data=self.store.save(p)
        self.assertEqual(len(data['history']),2);self.assertEqual(data['reviews'][0]['revision'],2)

    def test_concurrent_notes_only_one_accepts_same_revision(self):
        def submit(_):
            try:self.store.save(self.payload());return 'saved'
            except ApiError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
        self.assertEqual(sorted(results),['saved','site_review_revision_changed'])

    def test_layout_version_change_does_not_inherit_review(self):
        p=self.payload();self.store.save(p)
        raw=json.loads(DATA.read_text());raw['mapVersion']+=1
        for f in raw['features']:f['properties']['mapVersion']=raw['mapVersion']
        self.model.site=validate_site_map(raw)
        self.assertEqual(self.store.state()['reviews'],[]);self.assertEqual(self.store.state()['previousLayoutNotes'],1)
        with self.assertRaises(ApiError) as e:self.store.save(p)
        self.assertEqual(e.exception.code,'site_review_map_changed')

    def test_changed_geometry_same_version_also_invalidates_pending_review(self):
        p=self.payload();self.store.save(p);raw=json.loads(DATA.read_text())
        raw['features'][1]['geometry']['coordinates'][0]+=.00001;self.model.site=validate_site_map(raw)
        self.assertEqual(self.store.state()['reviews'],[])
        with self.assertRaises(ApiError):self.store.save(p)

    def test_refresh_date_alone_does_not_discard_same_layout_reviews(self):
        self.store.save(self.payload());raw=json.loads(DATA.read_text());raw['updatedAt']='2026-10-31T11:00:00+09:00';self.model.site=validate_site_map(raw)
        self.assertEqual(len(self.store.state()['reviews']),1)

    def test_invalid_review_cannot_mark_a_feature_verified(self):
        for changes in ({'status':'verified'},{'revision':True},{'evidenceKind':'sensor'},{'requestId':'bad'},{'note':''},{'reviewer':''},
                        {'status':'evidence_added','evidenceRef':''},{'featureId':'A-01'},{'geometryDigest':'stale'},{'mapVersion':True}):
            p=self.payload();p.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ApiError):self.store.save(p)
        self.assertEqual(self.store.state()['history'],[])

    def test_future_or_timezone_free_evidence_time_is_rejected(self):
        for value in ('2030-01-01T00:00:00Z','2026-10-31T10:00:00','no-date'):
            p=self.payload();p['observedAt']=value
            with self.assertRaises(ApiError):self.store.save(p)

    def test_evidence_note_keeps_source_and_kind_without_changing_occupancy(self):
        ledger=VacancyLedger(self.path,lambda:self.now);before=ledger.state('festival-demo')
        p=self.payload();p.update(status='evidence_added',evidenceKind='field_visit',evidenceRef='TEST-FIELD-RECORD-1');self.store.save(p)
        after=ledger.state('festival-demo');self.assertEqual(before['counts'],after['counts']);self.assertEqual(before['sequence'],after['sequence'])

    def test_snapshot_history_is_bounded_but_latest_per_feature_is_preserved(self):
        for n in range(55):
            p=self.payload();p['revision']=n;self.store.save(p)
        data=self.store.state();self.assertEqual(len(data['history']),50);self.assertEqual(data['reviews'][0]['revision'],55)
