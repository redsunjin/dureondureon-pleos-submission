from dataclasses import asdict,replace
import json
import unittest

from simulator import curb_parking as cp
from simulator import curb_replan as cr


class ReplanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene=cp.demo_scene();cls.plan=cp.plan(cls.scene).plan
        cls.baseline=cp.execute(cls.scene,cls.plan)

    def checkpoint(self,stage,actor=None,offset=10):
        first=next(i for i,f in enumerate(self.baseline['frames']) if f['stage']==stage and (actor is None or f['actor']==actor))
        i=min(first+offset,len(self.plan.commands)-1)
        return i,self.baseline['frames'][i]['poses']

    def validate(self,scene,plan,checkpoint):
        result=cp.execute(scene,plan,continuation=True)
        self.assertEqual(result['status'],'parked',result['reason'])
        self.assertEqual(result['frames'][0]['poses'],checkpoint)
        self.assertGreaterEqual(result['certified_body_clearance']+1e-8,scene.policy.separation)
        for frame in result['frames']:
            for c in self.scene.parked:
                self.assertTrue(c.pose.x-c.max_backward-1e-8<=frame['poses'][c.id]['x']<=c.pose.x+c.max_forward+1e-8)
        return result

    def test_space_creation_uses_new_search_from_current_pose_without_reset(self):
        i,poses=self.checkpoint('create_space','P-1')
        scene,plan,mode=cr.prepare(self.scene,self.plan,i,poses)
        self.assertEqual(mode,'current_pose_search')
        self.assertNotEqual(poses['P-1'],asdict(self.scene.parked[0].pose))
        self.validate(scene,plan,poses)

    def test_aligned_approach_replans_remaining_approach_not_initial_arrival(self):
        i,poses=self.checkpoint('approach',offset=30)
        scene,plan,mode=cr.prepare(self.scene,self.plan,i,poses)
        self.assertEqual(mode,'current_pose_search')
        self.assertGreater(scene.incoming.pose.x,self.scene.incoming.pose.x)
        self.validate(scene,plan,poses)

    def test_both_reverse_arcs_continue_from_the_actual_turned_pose(self):
        for stage in ('reverse_in','reverse_align'):
            i,poses=self.checkpoint(stage,offset=20)
            scene,plan,mode=cr.prepare(self.scene,self.plan,i,poses)
            self.assertEqual(mode,'remaining_path_revalidated')
            self.assertGreater(scene.incoming.pose.yaw,0.01)
            self.assertEqual(plan.commands[0].stage,'steering_at_rest')
            result=self.validate(scene,plan,poses)
            self.assertLess(result['elapsed_seconds'],self.baseline['elapsed_seconds'])

    def test_mid_settling_keeps_already_parked_new_car_stationary(self):
        i,poses=self.checkpoint('settle_spacing','P-3',offset=10)
        scene,plan,mode=cr.prepare(self.scene,self.plan,i,poses)
        result=self.validate(scene,plan,poses)
        self.assertTrue(all(f['poses']['NEW']==poses['NEW'] for f in result['frames']))
        self.assertFalse(any(c.actor=='NEW' and abs(c.distance)>1e-8 for c in plan.commands))

    def test_multiple_replans_preserve_the_original_absolute_movement_envelope(self):
        i,poses=self.checkpoint('create_space','P-1',offset=10)
        scene,plan,_=cr.prepare(self.scene,self.plan,i,poses)
        result=self.validate(scene,plan,poses)
        j=next(i for i,f in enumerate(result['frames']) if f['stage']=='reverse_in')+10
        checkpoint=result['frames'][j]['poses']
        scene2,plan2,_=cr.prepare(scene,plan,j,checkpoint)
        self.validate(scene2,plan2,checkpoint)
        for original,current in zip(self.scene.parked,scene2.parked):
            self.assertAlmostEqual(original.pose.x-original.max_backward,current.pose.x-current.max_backward)
            self.assertAlmostEqual(original.pose.x+original.max_forward,current.pose.x+current.max_forward)

    def test_checkpoint_geometry_cannot_be_substituted(self):
        i,poses=self.checkpoint('reverse_in')
        altered=json.loads(json.dumps(poses));altered['NEW']['x']+=0.2
        with self.assertRaisesRegex(ValueError,'checkpoint'):
            cr.prepare(self.scene,self.plan,i,altered)

    def test_unknown_participants_changed_signature_and_finished_path_are_refused(self):
        i,poses=self.checkpoint('create_space')
        for scene,plan,count,checkpoint in ((self.scene,replace(self.plan,scene_digest='bad'),i,poses),
                (self.scene,self.plan,len(self.plan.commands),poses),
                (self.scene,self.plan,i,{k:v for k,v in poses.items() if k!='NEW'})):
            with self.assertRaises(ValueError):cr.prepare(scene,plan,count,checkpoint)

    def test_fresh_planner_still_rejects_arbitrary_turned_initial_vehicle(self):
        scene=replace(self.scene,incoming=replace(self.scene.incoming,pose=cp.Pose(5,4.55,0.4)))
        self.assertEqual(cp.plan(scene).reason,'unsupported_initial_heading')

    def test_metadata_survives_json_storage_without_changing_poses(self):
        scene=cr.scene_from_dict(json.loads(json.dumps(asdict(self.scene))))
        plan=cr.plan_from_dict(json.loads(json.dumps(asdict(self.plan))))
        i,poses=self.checkpoint('reverse_align')
        new_scene,new_plan,_=cr.prepare(scene,plan,i,poses)
        self.validate(new_scene,new_plan,poses)


if __name__=='__main__':unittest.main()
