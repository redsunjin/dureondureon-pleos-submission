from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from coordination.site_map import SiteMap, validate_site_map
from coordination.server import Server, Ledger

DATA = Path(__file__).parent/'data'/'sejong-arboretum.geojson'


class SiteMapTests(unittest.TestCase):
    def setUp(self):
        self.raw=json.loads(DATA.read_text())

    def test_public_site_has_provenance_but_no_occupancy_authority(self):
        site=validate_site_map(self.raw)
        self.assertEqual(site['siteId'],'sejong-arboretum-review')
        self.assertTrue(site['previewOnly'])
        self.assertEqual(len(site['features']),19)
        self.assertTrue(all(f['properties']['verification']=='unverified' for f in site['features']))
        self.assertTrue(all(f['properties']['accuracyMeters'] is None for f in site['features']))
        self.assertNotIn('assignments',site)

    def test_external_properties_cannot_create_a_space_binding_or_sdk_url(self):
        self.raw['previewOnly']=False
        self.raw['publicKey']='do-not-expose'
        self.raw['features'][0]['properties'].update(spaceId='A-01',occupancy='vacant',available=True,sdkUrl='https://bad.example')
        site=validate_site_map(self.raw)
        self.assertTrue(site['previewOnly'])
        self.assertNotIn('publicKey',site)
        self.assertNotIn('spaceId',site['features'][0]['properties'])
        self.assertNotIn('available',site['features'][0]['properties'])

    def test_osm_requires_no_key_and_does_not_expose_kakao_key(self):
        config=SiteMap(DATA,public_key='a'*32).public_config()
        self.assertEqual(config['state'],'ready')
        self.assertEqual(config['provider'],'osm')
        self.assertIsNone(config['publicKey'])
        self.assertEqual(config['tileTemplate'],'https://tile.openstreetmap.org/{z}/{x}/{y}.png')

    def test_missing_site_and_missing_kakao_key_are_distinct(self):
        self.assertEqual(SiteMap().public_config()['state'],'missing_site')
        self.assertEqual(SiteMap(DATA,provider='kakao').public_config()['state'],'missing_key')
        self.assertFalse(SiteMap(DATA,provider='kakao',public_key='secret-rest-key').enabled)
        self.assertEqual(SiteMap(DATA,provider='kakao',public_key='a'*32).public_config()['state'],'ready')

    def test_mismatched_or_noninteger_versions_are_rejected(self):
        for value in (self.raw['mapVersion']+1,True,float(self.raw['mapVersion'])):
            with self.subTest(value=value):
                raw=deepcopy(self.raw);raw['features'][0]['properties']['mapVersion']=value
                with self.assertRaises(ValueError):validate_site_map(raw)

    def test_demo_event_is_not_a_real_site(self):
        self.raw['siteId']='festival-demo'
        with self.assertRaises(ValueError):validate_site_map(self.raw)

    def test_swapped_nonfinite_boolean_and_out_of_range_positions_are_rejected(self):
        for point in ([36.49,127.28],[127.2,float('nan')],[True,36.5],[181,36.5]):
            raw=deepcopy(self.raw);raw['features'][1]['geometry']['coordinates']=point
            with self.subTest(point=point),self.assertRaises(ValueError):validate_site_map(raw)

    def test_verified_geometry_requires_audit_and_accuracy(self):
        p=self.raw['features'][0]['properties'];p['verification']='verified'
        with self.assertRaises(ValueError):validate_site_map(self.raw)
        p.update(accuracyMeters=1,verifiedBy='FIELD-1',verifiedAt='2026-10-30T10:00:00+09:00')
        self.assertEqual(validate_site_map(self.raw)['features'][0]['properties']['verifiedBy'],'FIELD-1')

    def test_ring_must_be_closed_nonzero_and_not_self_intersecting(self):
        rings=[[[127,36],[127.01,36],[127.01,36.01],[127,36.01]],
               [[127,36],[127.01,36],[127.02,36],[127,36]],
               [[127,36],[127.01,36.01],[127.01,36],[127,36.01],[127,36]]]
        for value in rings:
            raw=deepcopy(self.raw);raw['features'][0]['geometry']['coordinates']=[value]
            with self.subTest(ring=value),self.assertRaises(ValueError):validate_site_map(raw)

    def test_holes_and_unknown_geometry_are_rejected_instead_of_silently_filled(self):
        raw=deepcopy(self.raw);raw['features'][0]['geometry']['coordinates']*=2
        with self.assertRaises(ValueError):validate_site_map(raw)
        self.raw['features'][0]['geometry']['type']='MultiPolygon'
        with self.assertRaises(ValueError):validate_site_map(self.raw)

    def test_duplicate_ids_missing_sources_and_unlicensed_data_are_rejected(self):
        cases=[]
        raw=deepcopy(self.raw);raw['features'][1]['id']=raw['features'][0]['id'];cases.append(raw)
        raw=deepcopy(self.raw);raw['source']='';cases.append(raw)
        raw=deepcopy(self.raw);raw['usageRights']='unknown';cases.append(raw)
        raw=deepcopy(self.raw);raw['features'][0]['properties']['sourceUrl']='javascript:alert(1)';cases.append(raw)
        raw=deepcopy(self.raw);raw['features'][0]['properties']['sourceUpdatedAt']='2026-10-30';cases.append(raw)
        for raw in cases:
            with self.assertRaises(ValueError):validate_site_map(raw)

    def test_unbounded_site_extent_is_rejected(self):
        self.raw['features'][1]['geometry']['coordinates']=[128,37]
        with self.assertRaises(ValueError):validate_site_map(self.raw)

    def test_oversize_and_invalid_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'site.json'
            for content in (' '*1_000_001,'{"type":NaN}','[]'):
                path.write_text(content)
                with self.assertRaises(ValueError):SiteMap(path)

    def test_client_mutation_does_not_modify_server_map(self):
        model=SiteMap(DATA);copy=model.public_config();copy['site']['features'].clear()
        self.assertEqual(len(model.public_config()['site']['features']),19)


class SiteMapHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=Server(('127.0.0.1',0),Ledger(Path(self.temp.name)/'ledger.db'),SiteMap(DATA))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def test_config_and_vendor_files_are_local_allowlisted(self):
        with urlopen(self.base+'/v1/site-map') as response:
            data=json.load(response);self.assertEqual(data['state'],'ready');self.assertIsNone(data['publicKey'])
        for path,content in [('site-map.js','text/javascript'),('kakao-map.js','text/javascript'),('osm-map.js','text/javascript'),('vendor/leaflet/leaflet.js','text/javascript'),('vendor/leaflet/leaflet.css','text/css')]:
            with urlopen(self.base+'/admin/'+path) as response:
                self.assertIn(content,response.headers['Content-Type'])
                policy=response.headers['Content-Security-Policy']
                self.assertIn('https://tile.openstreetmap.org',policy)
                self.assertNotIn('https://dapi.kakao.com',policy)
        for path in ('vendor/leaflet/LICENSE','../site_map.py'):
            with self.assertRaises(HTTPError):urlopen(self.base+'/admin/'+path)

    def test_foreign_origin_cannot_read_site_or_key(self):
        for headers in ({'Origin':'https://other.example'},{'Host':'other.example'}):
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/v1/site-map',headers=headers))
            self.assertEqual(error.exception.code,403)

    def test_review_http_roundtrip_replay_and_origin_guards(self):
        site=self.server.site_map.site
        payload=dict(siteId=site['siteId'],mapVersion=site['mapVersion'],geometryDigest=site['geometryDigest'],featureId=site['features'][0]['id'],
                     requestId=str(uuid.uuid4()),revision=0,status='needs_visit',reviewer='HTTP-QA',evidenceKind='document_review',
                     observedAt=datetime.now(timezone.utc).isoformat(),note='HTTP 시험 · 현장 미확인',evidenceRef='')
        url=self.base+'/v1/site-map/reviews';raw=json.dumps(payload).encode()
        for _ in range(2):
            with urlopen(Request(url,raw,{'Content-Type':'application/json'})) as response:
                data=json.load(response);self.assertEqual(len(data['history']),1);self.assertEqual(data['reviews'][0]['revision'],1)
        with urlopen(url) as response:self.assertEqual(json.load(response)['reviews'],data['reviews'])
        with self.assertRaises(HTTPError) as error:urlopen(Request(url,raw,{'Content-Type':'application/json','Origin':'https://other.example'}))
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:urlopen(Request(url,raw,{'Content-Type':'text/plain'}))
        self.assertEqual(error.exception.code,415)

    def test_survey_download_and_stale_layout_origin_guards(self):
        digest=self.server.site_map.site['geometryDigest']
        url=self.base+'/v1/site-map/survey?layout='+digest
        before=self.server.site_reviews.state()
        with urlopen(url) as response:
            self.assertIn('attachment;',response.headers['Content-Disposition'])
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn("default-src 'none'",response.headers['Content-Security-Policy'])
            self.assertEqual(response.read().decode().count('<section>'),19)
        self.assertEqual(before,self.server.site_reviews.state())
        for target,headers,code in ((url,{'Origin':'https://other.example'},403),(self.base+'/v1/site-map/survey?layout=old',{},409)):
            with self.assertRaises(HTTPError) as error:urlopen(Request(target,headers=headers))
            self.assertEqual(error.exception.code,code)

    def test_review_does_not_change_demo_ledger_or_accept_writes(self):
        before=self.server.vacancy.state('festival-demo')
        with urlopen(self.base+'/v1/site-map') as response:self.assertTrue(json.load(response)['previewOnly'])
        req=Request(self.base+'/v1/site-map',b'{}',{'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:urlopen(req)
        self.assertEqual(error.exception.code,404)
        after=self.server.vacancy.state('festival-demo')
        self.assertEqual(before['counts'],after['counts']);self.assertEqual(after['sequence'],0)

    def test_kakao_policy_requires_both_site_and_public_key(self):
        for key,enabled in (('',False),('a'*32,True)):
            self.server.site_map=SiteMap(DATA,provider='kakao',public_key=key)
            with urlopen(self.base+'/admin/') as response:
                policy=response.headers['Content-Security-Policy']
                self.assertEqual('https://dapi.kakao.com' in policy,enabled)
                self.assertNotIn('tile.openstreetmap.org',policy)
                self.assertNotIn("script-src 'self' 'unsafe-inline'",policy)
