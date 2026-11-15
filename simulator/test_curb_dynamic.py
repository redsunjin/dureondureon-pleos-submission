from dataclasses import replace
import unittest
from simulator import curb_parking as cp, curb_dynamic as cd


class ChangedSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene=cp.demo_scene();cls.proposal=cp.plan(cls.scene).plan
        cls.original=cp.execute(cls.scene,cls.proposal)
        cls.index=next(i for i,f in enumerate(cls.original['frames']) if f['time']>=48)
        cls.poses=cls.original['frames'][cls.index]['poses']

    def changed(self,poses=None,obstacles=()):
        return cd.observed_scene(self.scene,poses or self.poses,obstacles)

    def validate(self,scene):
        plan=cd.plan(scene,2);result=cp.execute(scene,plan,continuation=True)
        self.assertEqual(result['status'],'parked',result['reason'])
        self.assertEqual(result['frames'][0]['poses'],{c.id:vars(c.pose) for c in (*scene.parked,scene.incoming)})
        self.assertGreaterEqual(result['certified_body_clearance']+1e-8,scene.policy.separation)
        self.assertGreaterEqual(result['certified_wall_clearance']+1e-8,scene.policy.boundary)
        for f in result['frames']:
            for original in self.scene.parked:
                self.assertGreaterEqual(f['poses'][original.id]['x']+1e-8,original.pose.x-original.max_backward)
                self.assertLessEqual(f['poses'][original.id]['x']-1e-8,original.pose.x+original.max_forward)
        return plan,result

    def test_new_obstacle_blocks_old_controls_but_a_different_route_finishes(self):
        obstacle=cp.Obstacle('BOX',cp.Pose(16,5.9),.25,.25)
        scene=self.changed(obstacles=(obstacle,))
        old=replace(self.proposal,scene_digest=cp.signature(scene),created_at=0,commands=self.proposal.commands[self.index:])
        self.assertEqual(cp.execute(scene,old,continuation=True)['reason'],'clearance_guard')
        plan,result=self.validate(scene)
        self.assertNotEqual(plan.commands,old.commands)
        self.assertTrue(any(c.stage=='replan_move' and abs(c.distance)>0 for c in plan.commands))
        self.assertEqual(result['frames'][-1]['obstacles'],[{'id':'BOX','pose':vars(obstacle.pose),'length':.25,'width':.25}])

    def test_changed_incoming_position_and_heading_get_a_new_goal_connector(self):
        poses={i:dict(p) for i,p in self.poses.items()};poses['NEW']['y']+=.15;poses['NEW']['yaw']+=.03
        self.validate(self.changed(poses))

    def test_changed_parked_position_is_not_reset_to_its_old_target(self):
        poses={i:dict(p) for i,p in self.poses.items()};poses['P-3']['x']-=.12
        self.validate(self.changed(poses))

    def test_repeated_observations_do_not_expand_absolute_movement_envelopes(self):
        scene=self.changed();poses={c.id:vars(c.pose) for c in (*scene.parked,scene.incoming)}
        poses['P-3']={**poses['P-3'],'x':poses['P-3']['x']-.1}
        again=cd.observed_scene(scene,poses,())
        for old,new in zip(self.scene.parked,again.parked):
            self.assertAlmostEqual(old.pose.x-old.max_backward,new.pose.x-new.max_backward)
            self.assertAlmostEqual(old.pose.x+old.max_forward,new.pose.x+new.max_forward)

    def test_overlap_and_unsupported_parked_heading_are_holds_not_teleports(self):
        for label,poses,obstacles in [
            ('obstacle',self.poses,(cp.Obstacle('BLOCK',cp.Pose(**self.poses['NEW']),1,1),)),
            ('heading',{**self.poses,'P-1':{**self.poses['P-1'],'yaw':.1}},()),
            ('envelope',{**self.poses,'P-1':{**self.poses['P-1'],'x':.3}},())]:
            with self.subTest(label=label),self.assertRaises(ValueError):self.changed(poses,obstacles)

    def test_full_road_barrier_has_no_certified_continuation(self):
        scene=self.changed(obstacles=(cp.Obstacle('WALL',cp.Pose(13.5,3.75),.1,7.1),))
        with self.assertRaises(ValueError):cd.plan(scene,2)

    def test_new_controls_cannot_skip_the_independent_execution_guard(self):
        scene=self.changed();plan,_=self.validate(scene)
        bad=replace(plan,commands=(cp.Command('NEW',-15,0,50,'replan_move'),))
        self.assertNotEqual(cp.execute(scene,bad,continuation=True)['status'],'parked')


if __name__=='__main__':unittest.main()
