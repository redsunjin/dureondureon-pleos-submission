from dataclasses import asdict,replace
import math
import unittest
from simulator import curb_parking as cp,curb_traffic as ct
from coordination.curb_sessions import build_template


class TrafficGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.data=build_template()
    def model(self,scenario='pedestrian',now=100000):
        return ct.validate(ct.scripted(scenario,self.data,now),'scripted',now,30,7.5,4)

    def test_pedestrian_future_crossing_stops_before_current_body_overlap(self):
        t=self.model();result=ct.forecast(self.data,40,t,100000,4)
        self.assertEqual(result['safety'],'risk');self.assertGreater(result['timeToRiskSeconds'],0)
        f=max((f for f in self.data['frames'] if f['time']<=40),key=lambda f:f['time'])
        scene=cp.demo_scene();ped=t['tracks'][0]
        for car in (*scene.parked,scene.incoming):
            self.assertGreater(cp.distance(cp.corners(car,cp.Pose(**f['poses'][car.id])),cp.corners(cp.Obstacle('ped',cp.Pose(**ped['pose']),.6,.6))),.4)

    def test_approaching_vehicle_is_detected_but_one_that_has_passed_is_clear(self):
        t=self.model('vehicle')
        self.assertEqual(ct.forecast(self.data,32,t,100000,4)['safety'],'risk')
        self.assertEqual(ct.forecast(self.data,32,t,106000,4)['safety'],'clear')
        self.assertEqual(ct.live_tracks(t,106000,4),[])

    def test_far_moving_object_does_not_stop_a_clear_route(self):
        t=self.model('vehicle');t['tracks'][0]['pose']['y']=20
        self.assertEqual(ct.forecast(self.data,40,t,100000,4)['safety'],'clear')

    def test_live_coverage_expires_even_when_it_reports_no_objects(self):
        for tracks in ([],[{**self.model()['tracks'][0],'durationSeconds':0}]):
            t=ct.validate(tracks,'observed',100000,30,7.5,4)
            self.assertTrue(ct.stale(t,101501));self.assertEqual(ct.forecast(self.data,40,t,101501,4)['safety'],'stale')

    def test_script_cannot_disappear_inside_the_road(self):
        tracks=ct.scripted('pedestrian',self.data,100000);tracks[0]['durationSeconds']=1
        with self.assertRaisesRegex(ValueError,'exit'):ct.validate(tracks,'scripted',100000,30,7.5,4)

    def test_clock_rollback_does_not_authorize_clearance(self):
        self.assertEqual(ct.forecast(self.data,40,self.model(),99999,4)['safety'],'stale')

    def test_stale_measurement_cannot_hide_behind_a_new_receipt_timestamp(self):
        tracks=[{**self.model()['tracks'][0],'observedAtMillis':98000,'durationSeconds':0}]
        t=ct.validate(tracks,'observed',100000,30,7.5,4)
        self.assertEqual(ct.forecast(self.data,40,t,100000,4)['safety'],'stale')

    def test_invalid_speed_dimensions_timestamps_ids_and_kinds_are_refused(self):
        base=self.model()['tracks'][0]
        for patch in ({'vx':float('nan')},{'vx':11},{'width':-1},{'kind':'unknown'},{'observedAtMillis':100001},{'id':[]},{'id':'한글'}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):ct.validate([{**base,**patch}],'scripted',100000,30,7.5,4)
        with self.assertRaises(ValueError):ct.validate([base,base],'scripted',100000,30,7.5,4)

    def test_relative_sweep_catches_corner_crossing_between_clear_endpoints(self):
        car=cp.Car('NEW',cp.Pose(10,4),length=2,width=2,wheelbase=1,rear_overhang=.2)
        scene=cp.Scene('corner',(),car,parking_y=1.5)
        poses={'NEW':asdict(car.pose)}
        data={'scene':asdict(scene),'plan':{'commands':[asdict(cp.Command('NEW',0,0,.1,'steering_at_rest'))]},
            'frames':[{'time':0,'poses':poses},{'time':.1,'poses':poses},{'time':.1,'poses':poses}]}
        track={'id':'CUT','kind':'pedestrian','pose':{'x':10.7,'y':5.2,'yaw':0},'length':.05,'width':.05,
            'vx':5,'vy':-5,'observedAtMillis':100000,'durationSeconds':0}
        for dt in (0,.1):
            p=cp.Pose(10.7+5*dt,5.2-5*dt)
            self.assertGreater(cp.distance(cp.corners(car),cp.corners(cp.Obstacle('p',p,.05,.05))),.1)
        midpoint=cp.Pose(10.95,4.95)
        self.assertEqual(cp.distance(cp.corners(car),cp.corners(cp.Obstacle('p',midpoint,.05,.05))),0)
        traffic=ct.validate([track],'observed',100000,30,7.5,4)
        self.assertEqual(ct.forecast(data,0,traffic,100000,4)['safety'],'risk')

    def test_stationary_participants_are_also_checked(self):
        t=self.model();t['tracks'][0].update(pose={'x':3.8,'y':3.5,'yaw':0},vx=0,vy=-.3)
        self.assertEqual(ct.forecast(self.data,0,t,100000,4)['safety'],'risk')


if __name__=='__main__':unittest.main()
