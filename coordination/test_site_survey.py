from copy import deepcopy
import unittest
from coordination.site_map import SiteMap
from coordination.site_survey import render_survey
from coordination.test_site_reviews import DATA

class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.site=SiteMap(DATA).site
        self.reviews={k:self.site[k] for k in ('siteId','mapVersion','geometryDigest')}
        self.reviews.update(reviews=[],previousLayoutNotes=2)

    def test_complete_printable_standalone_sheet(self):
        html=render_survey(self.site,self.reviews,0)
        self.assertEqual(html.count('<section>'),19)
        for f in self.site['features']:self.assertIn(f['id'],html)
        self.assertIn('break-before:page',html)
        self.assertIn(self.site['geometryDigest'],html)
        self.assertIn('이전 배치 기록 2건은 포함하지 않습니다',html)
        self.assertNotIn('<script',html)
        self.assertNotIn('<img',html)

    def test_escape_untrusted_notes_and_metadata(self):
        site=deepcopy(self.site);site['label']='<script>alert(1)</script>'
        self.reviews['reviews']=[dict(featureId=site['features'][0]['id'],status='needs_visit',evidenceKind='document_review',reviewer='<img src=x>',revision=1,observedAt='2026-11-03T10:00:00+09:00',evidenceRef='<b>source</b>',note='<script>bad</script>')]
        html=render_survey(site,self.reviews,0)
        self.assertNotIn('<script>',html);self.assertNotIn('<img src',html)
        self.assertIn('&lt;script&gt;bad',html)
        self.assertIn('자료 검토',html)

    def test_reject_previous_layout(self):
        self.reviews['geometryDigest']='old'
        with self.assertRaises(ValueError):render_survey(self.site,self.reviews,0)
