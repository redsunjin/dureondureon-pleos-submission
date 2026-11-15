import unittest
from coordination import test_curb_sessions as base
from coordination.errors import ApiError
from coordination.curb_sessions import HEARTBEAT_MS, CurbSessions

class ParticipationTests(unittest.TestCase):
    setUpClass=classmethod(base.CurbTests.setUpClass.__func__)
    setUp=base.CurbTests.setUp
    tearDown=base.CurbTests.tearDown
    create=base.CurbTests.create
    act=base.CurbTests.act
    def setup_mode(self):
        s=self.create()
        setup=self.ledger.participation(s['sessionId'],'V-07')
        self.tokens={r['vehicleId']:r['token'] for r in setup['invitations']}
        self.s=setup['session']
        return self.s

    def respond(self,car,action='approve',request=None,revision=None,move=None):
        self.sequence+=1
        p=self.ledger.participant_poll(self.tokens[car])
        return self.ledger.participant_act(self.tokens[car],request or f'response-{self.sequence}',revision or p['session']['revision'],action,move or p['participant']['moveRequestId'])

    def test_scoped_response_and_owner_cannot_impersonate(self):
        s=self.setup_mode();car=next(iter(self.tokens))
        with self.assertRaises(ApiError) as e:self.act(s,'approve',car)
        self.assertEqual(e.exception.status,403)
        with self.assertRaises(ApiError):self.respond(car,move=f"{s['sessionId']}_NOT-MY-CAR")
        p=self.respond(car)
        self.assertEqual(p['participant']['vehicleId'],car);self.assertNotIn('participants',p['session'])
        self.assertEqual(p['approvedCount'],1)
        with self.assertRaises(ApiError):self.ledger.participant_act(self.tokens[car],'start',p['session']['revision'],'start',p['participant']['moveRequestId'])

    def test_all_responses_explicit_start_and_withdraw(self):
        self.setup_mode()
        for car in self.tokens:p=self.respond(car)
        self.assertEqual(p['session']['status'],'ready')
        s=self.ledger.state('V-07')['session'];s=self.act(s,'start')
        self.assertEqual(s['status'],'running')
        p=self.respond(next(iter(self.tokens)),'withdraw')
        self.assertEqual(p['session']['status'],'stopped')
        self.assertEqual(p['session']['reason'],'consent_withdrawn')

    def test_gap_stops_before_advance_and_reconnect_never_resumes(self):
        self.setup_mode()
        for car in self.tokens:self.respond(car)
        s=self.act(self.ledger.state('V-07')['session'],'start')
        elapsed=s['elapsedSeconds'];self.now+=HEARTBEAT_MS+1
        p=self.ledger.participant_poll(next(iter(self.tokens.values())))
        s=self.ledger.state('V-07')['session']
        self.assertEqual(s['status'],'stopped');self.assertEqual(s['elapsedSeconds'],elapsed)
        self.assertTrue(all(c['decision']=='pending' for c in s['participants']))
        self.assertEqual(p['session']['status'],'stopped')

    def test_other_polling_does_not_hide_one_missing_vehicle(self):
        self.setup_mode()
        for car in self.tokens:self.respond(car)
        absent=next(iter(self.tokens));self.now+=2000
        for car,t in self.tokens.items():
            if car!=absent:self.ledger.participant_poll(t)
        self.now+=1001
        self.assertEqual(self.ledger.state('V-07')['session']['reason'],'participant_connection_lost')

    def test_response_replay_and_changed_request(self):
        self.setup_mode();car=next(iter(self.tokens));p=self.respond(car,request='same')
        old=p['session']['revision']-1
        again=self.ledger.participant_act(self.tokens[car],'same',old,'approve',p['participant']['moveRequestId'])
        self.assertEqual(again['session']['revision'],p['session']['revision'])
        with self.assertRaises(ApiError):self.ledger.participant_act(self.tokens[car],'same',old,'reject',p['participant']['moveRequestId'])

    def test_restart_retains_mode_and_identity(self):
        self.setup_mode();token=next(iter(self.tokens.values()))
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
        p=self.ledger.participant_poll(token)
        self.assertEqual(p['session']['sessionId'],self.s['sessionId'])
        self.assertTrue(self.ledger.state('V-07')['session']['participantMode'])

    def test_invalid_token_rejected_and_no_poll_no_approval(self):
        self.setup_mode();token=next(iter(self.tokens.values()))
        with self.assertRaises(ApiError):self.ledger.participant_poll('invalid')
        with self.assertRaises(ApiError) as e:self.ledger.participant_act(token,'no-poll',self.s['revision'],'approve','anything')
        self.assertEqual(e.exception.code,'participant_poll_required')

    def test_replan_requires_new_links_and_old_receipt_stays_old(self):
        s=self.setup_mode();car=next(iter(self.tokens));old_token=self.tokens[car]
        p=self.respond(car,request='original-response');old_revision=p['session']['revision']-1;move=p['participant']['moveRequestId']
        self.respond(car,'withdraw')
        stopped=self.ledger.state('V-07')['session']
        replanned=self.ledger.replan(stopped['sessionId'],'V-07','replan',stopped['revision'])['session']
        self.assertTrue(replanned['participantMode'])
        with self.assertRaises(ApiError):self.act(replanned,'approve',car)
        replay=self.ledger.participant_act(old_token,'original-response',old_revision,'approve',move)
        self.assertEqual(replay['session']['sessionId'],s['sessionId'])
        links=self.ledger.participation(replanned['sessionId'],'V-07')['invitations']
        self.assertNotIn(old_token,[p['token'] for p in links])
        self.assertTrue(all(p['decision']=='pending' for p in replanned['participants']))

    def test_participant_poll_does_not_advance_owner_clock(self):
        self.setup_mode()
        for car in self.tokens:self.respond(car)
        s=self.act(self.ledger.state('V-07')['session'],'start')
        for _ in range(4):
            self.now+=1000
            for token in self.tokens.values():p=self.ledger.participant_poll(token)
        self.assertEqual(p['session']['reason'],'connection_lost')
        self.assertEqual(self.ledger.state('V-07')['session']['elapsedSeconds'],s['elapsedSeconds'])

class ParticipationHttpTests(unittest.TestCase):
    def test_http_identity_response_and_origin(self):
        import json
        import tempfile
        import threading
        from pathlib import Path
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from coordination.server import Server,Ledger
        with tempfile.TemporaryDirectory() as folder:
            server=Server(('127.0.0.1',0),Ledger(Path(folder)/'state.db'))
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base_url=f'http://127.0.0.1:{server.server_port}'
            def call(path,body,headers=None):
                with urlopen(Request(base_url+path,json.dumps(body).encode(),headers or {'Content-Type':'application/json'})) as r:return json.load(r)
            try:
                s=call('/v1/curb/sessions',dict(ownerVehicleId='HTTP-OWNER',requestId='create'))['session']
                setup=call('/v1/curb/participation',dict(sessionId=s['sessionId'],ownerVehicleId='HTTP-OWNER'))
                token=setup['invitations'][0]['token']
                p=call('/v1/curb/participant/poll',dict(token=token))
                response=dict(token=token,requestId='approve',revision=p['session']['revision'],action='approve',moveRequestId=p['participant']['moveRequestId'])
                p=call('/v1/curb/participant/respond',response)
                self.assertEqual(p['participant']['decision'],'approved');self.assertEqual(p['approvedCount'],1)
                for headers,code in (({'Content-Type':'application/json','Origin':'https://foreign.example'},403),({'Content-Type':'text/plain'},415)):
                    with self.assertRaises(HTTPError) as err:call('/v1/curb/participant/poll',dict(token=token),headers)
                    self.assertEqual(err.exception.code,code)
                with urlopen(base_url+'/participate/') as r:self.assertIn('차량별 협력 주차 참여',r.read().decode())
            finally:server.shutdown();server.server_close();worker.join()

    def test_five_http_clients_conflicts_loss_and_reconnection(self):
        """Actual HTTP clients, with only the server clock accelerated for a gap."""
        import json,tempfile,threading
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from coordination.server import Server,Ledger
        now=[1_000_000]
        with tempfile.TemporaryDirectory() as folder:
            server=Server(('127.0.0.1',0),Ledger(Path(folder)/'state.db',lambda:now[0]))
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            url=f'http://127.0.0.1:{server.server_port}'
            def post(path,body):
                with urlopen(Request(url+path,json.dumps(body).encode(),{'Content-Type':'application/json'})) as r:return json.load(r)
            def owner():return server.curb.state('MULTI-HTTP')['session']
            try:
                s=post('/v1/curb/sessions',dict(ownerVehicleId='MULTI-HTTP',requestId='create'))['session']
                links=post('/v1/curb/participation',dict(ownerVehicleId='MULTI-HTTP',sessionId=s['sessionId']))['invitations']
                snapshots={p['vehicleId']:post('/v1/curb/participant/poll',dict(token=p['token'])) for p in links}
                barrier=threading.Barrier(5)
                def simultaneous(link):
                    p=snapshots[link['vehicleId']];barrier.wait()
                    body=dict(token=link['token'],requestId='vote-'+link['vehicleId'],revision=p['session']['revision'],moveRequestId=p['participant']['moveRequestId'],action='approve')
                    try:post('/v1/curb/participant/respond',body);return 200
                    except HTTPError as e:
                        code=e.code;e.close();return code
                with ThreadPoolExecutor(5) as pool:self.assertEqual(sorted(pool.map(simultaneous,links)),[200,409,409,409,409])
                for link in links:
                    p=post('/v1/curb/participant/poll',dict(token=link['token']))
                    if p['participant']['decision']=='pending':
                        body=dict(token=link['token'],requestId='explicit-'+link['vehicleId'],revision=p['session']['revision'],moveRequestId=p['participant']['moveRequestId'],action='approve')
                        post('/v1/curb/participant/respond',body)  # Discard response, then retry same UUID.
                        self.assertEqual(post('/v1/curb/participant/respond',body)['participant']['decision'],'approved')
                s=owner();self.assertEqual(s['status'],'ready');self.assertEqual(s['revision'],7)
                post('/v1/curb/sessions/'+s['sessionId']+'/actions',dict(ownerVehicleId='MULTI-HTTP',requestId='start',revision=s['revision'],action='start'))
                for _ in range(4):
                    now[0]+=1000
                    for link in links[1:]:post('/v1/curb/participant/poll',dict(token=link['token']))
                    owner()
                s=owner();self.assertEqual(s['reason'],'participant_connection_lost')
                poses={p['vehicleId']:p['pose'] for p in s['participants']}
                p=post('/v1/curb/participant/poll',dict(token=links[0]['token']))
                self.assertEqual(p['session']['status'],'stopped');self.assertEqual(p['approvedCount'],0)
                self.assertEqual(poses,{p['vehicleId']:p['pose'] for p in owner()['participants']})
            finally:server.shutdown();server.server_close();worker.join()
