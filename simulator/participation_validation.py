"""Device-free multi-client protocol simulation against the production SQLite authority.

Virtual time drives network gaps; no Android UI, sensors or physical actuation.
Run: python3 -m simulator.participation_validation --seeds 20 --output output/participation-simulation/results.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import tempfile
import threading

from coordination.curb_sessions import CurbSessions, build_template, HEARTBEAT_MS, APPROVAL_MS
from coordination.errors import ApiError


class Network:
    def __init__(self,folder,template,seed):
        self.now=1_000_000;self.random=random.Random(seed);self.counter=0;self.events=[]
        self.path=Path(folder)/'state.db';self.template=template
        self.server=CurbSessions(self.path,lambda:self.now,lambda:template)
        s=self.server.create('SIM-OWNER',self.uid())['session']
        self.invite(s)

    def uid(self):
        self.counter+=1;return f'sim-{self.counter}'

    def invite(self,s):
        setup=self.server.participation(s['sessionId'],'SIM-OWNER')
        self.clients={p['vehicleId']:dict(token=p['token'],state=None) for p in setup['invitations']}
        self.session=setup['session']

    def owner(self):return self.server.state('SIM-OWNER')['session']

    def poll(self,car):
        c=self.clients[car];c['state']=self.server.participant_poll(c['token']);return c['state']

    def response(self,car,action='approve',snapshot=None,request=None):
        p=snapshot or self.poll(car)
        return self.server.participant_act(self.clients[car]['token'],request or self.uid(),p['session']['revision'],action,p['participant']['moveRequestId'])

    def all_approve(self):
        cars=list(self.clients);self.random.shuffle(cars)
        for car in cars:self.response(car)
        assert self.owner()['status']=='ready'

    def act(self,action):
        s=self.owner();return self.server.act(s['sessionId'],'SIM-OWNER',self.uid(),s['revision'],action)['session']

    def tick(self,delta=1000,omit=None,owner=True):
        self.now+=delta
        cars=list(self.clients);self.random.shuffle(cars)
        for car in cars:
            if car!=omit:self.poll(car)
        return self.owner() if owner else None

    def frozen(self,s):
        poses={p['vehicleId']:p['pose'] for p in s['participants']}
        self.tick(1000)
        after=self.owner()
        assert after['status']=='stopped'
        assert {p['vehicleId']:p['pose'] for p in after['participants']}==poses
        return after


def complete(n):
    n.all_approve();n.act('start')
    for _ in range(100):
        s=n.tick(n.random.randint(600,1100))
        if s['status']=='completed':break
        assert s['status']=='running'
    assert s['status']=='completed'
    assert all(p['progressPercent']==100 for p in s['participants'])
    return {'status':s['status'],'elapsed':s['elapsedSeconds']}


def simultaneous(n):
    cars=list(n.clients)
    for car in cars:n.poll(car)
    barrier=threading.Barrier(len(cars))
    snapshots={car:n.clients[car]['state'] for car in cars}
    requests={car:n.uid() for car in cars}
    def send(car):
        barrier.wait()
        try:n.response(car,snapshot=snapshots[car],request=requests[car]);return (car,200)
        except ApiError as e:return (car,e.status)
    with ThreadPoolExecutor(5) as pool:results=list(pool.map(send,cars))
    assert sorted(code for _,code in results)==[200,409,409,409,409]
    # A fresh read alone never casts a rejected response again.
    assert sum(p['decision']=='approved' for p in n.owner()['participants'])==1
    for car,code in results:
        if code==409:n.response(car)
    assert n.owner()['status']=='ready'
    return {'accepted':1,'explicit_retries':4,'status':'ready'}


def one_lost(n):
    n.all_approve();n.act('start');n.tick()
    missing=n.random.choice(list(n.clients))
    for _ in range(4):s=n.tick(1000,omit=missing)
    assert s['status']=='stopped' and s['reason']=='participant_connection_lost'
    assert all(p['decision']=='pending' for p in s['participants'])
    n.poll(missing);n.frozen(s)
    return {'missing':missing,'reason':s['reason'],'reconnected_status':'stopped'}


def withdrawal(n):
    n.all_approve();n.act('start');n.tick()
    car=n.random.choice(list(n.clients));p=n.response(car,'withdraw');s=n.owner()
    assert p['session']['status']=='stopped' and s['reason']=='consent_withdrawn'
    n.frozen(s)
    return {'vehicle':car,'reason':s['reason']}


def refuse(n):
    car=n.random.choice(list(n.clients));n.response(car,'reject')
    s=n.owner();assert s['status']=='rejected'
    try:n.act('start');raise AssertionError('started rejected plan')
    except ApiError:pass
    return {'status':s['status']}


def lost_ack(n):
    car=n.random.choice(list(n.clients));p=n.poll(car);req=n.uid()
    n.response(car,snapshot=p,request=req)  # Transport discards the successful reply.
    n.now+=n.random.randint(10,900);n.poll(car)
    replay=n.response(car,snapshot=p,request=req)
    assert replay['participant']['decision']=='approved'
    s=n.owner();assert sum(p['decision']=='approved' for p in s['participants'])==1
    assert s['revision']==3
    return {'duplicates':0,'revision':s['revision']}


def owner_lost(n):
    n.all_approve();n.act('start')
    for _ in range(4):n.tick(1000,owner=False)
    s=n.owner();assert s['reason']=='connection_lost' and s['elapsedSeconds']==0
    n.frozen(s)
    return {'reason':s['reason'],'elapsed':s['elapsedSeconds']}


def restart(n):
    n.all_approve();n.act('start');n.tick()
    before=n.owner();n.server=CurbSessions(n.path,lambda:n.now,lambda:n.template)
    s=n.owner();assert s['reason']=='server_restarted' and s['elapsedSeconds']==before['elapsedSeconds']
    n.frozen(s)
    return {'reason':s['reason'],'restored_status':'stopped'}


def boundary(n):
    car=next(iter(n.clients));n.response(car)
    n.now+=HEARTBEAT_MS
    assert n.owner()['status']=='awaiting'
    n.now+=1
    s=n.owner();assert s['reason']=='participant_connection_lost'
    return {'at_3000ms':'awaiting','at_3001ms':'stopped'}


def expire(n):
    p=n.poll(next(iter(n.clients)));n.now+=APPROVAL_MS+1
    s=n.owner();assert s['status']=='expired'
    try:n.response(next(iter(n.clients)),snapshot=p);raise AssertionError('accepted expired consent')
    except ApiError:pass
    return {'status':s['status']}


SCENARIOS={f.__name__:f for f in (complete,simultaneous,one_lost,withdrawal,refuse,lost_ack,owner_lost,restart,boundary,expire)}


def replan_check(template):
    with tempfile.TemporaryDirectory() as folder:
        n=Network(folder,template,0);n.all_approve();n.act('start');n.tick()
        old_clients=n.clients.copy();old_session=n.owner()
        n.response(next(iter(n.clients)),'withdraw');s=n.owner()
        child=n.server.replan(s['sessionId'],'SIM-OWNER',n.uid(),s['revision'])['session']
        assert child['parentSessionId']==old_session['sessionId'] and child['participantMode']
        assert all(p['decision']=='pending' for p in child['participants'])
        before={p['vehicleId']:p['pose'] for p in s['participants']}
        assert {p['vehicleId']:p['pose'] for p in child['participants']}==before
        n.invite(child)
        assert not ({c['token'] for c in n.clients.values()} & {c['token'] for c in old_clients.values()})
        for car,c in old_clients.items():
            p=n.server.participant_poll(c['token'])
            assert p['session']['sessionId']==s['sessionId'] and p['session']['status']=='stopped'
            try:n.server.participant_act(c['token'],n.uid(),child['revision'],'approve',f"{child['sessionId']}_{car}");raise AssertionError('old token approved new plan')
            except ApiError:pass
        assert n.owner()['status']=='awaiting'
        n.all_approve();n.act('start')
        return {'status':'running','new_consents':5,'poses_preserved':True,'old_tokens_rejected':5}


def run(seeds=20):
    template=build_template();results=[]
    for name,scenario in SCENARIOS.items():
        for seed in range(seeds):
            with tempfile.TemporaryDirectory() as folder:
                n=Network(folder,template,seed)
                try:detail=scenario(n);results.append(dict(scenario=name,seed=seed,passed=True,detail=detail))
                except Exception as e:results.append(dict(scenario=name,seed=seed,passed=False,error=f'{type(e).__name__}: {e}'))
        print(name, sum(r['passed'] for r in results if r['scenario']==name),'/',seeds,flush=True)
    try:results.append(dict(scenario='replan',seed=0,passed=True,detail=replan_check(template)))
    except Exception as e:results.append(dict(scenario='replan',seed=0,passed=False,error=f'{type(e).__name__}: {e}'))
    return dict(simulationOnly=True,model='5 independent protocol clients / real SQLite authority / virtual clock',seeds=seeds,
        passed=sum(r['passed'] for r in results),total=len(results),results=results)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seeds',type=int,default=20);parser.add_argument('--output',default='output/participation-simulation/results.json');args=parser.parse_args()
    if not 1<=args.seeds<=100:parser.error('seeds must be 1..100')
    result=run(args.seeds);path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(f"{result['passed']}/{result['total']}",flush=True)
    raise SystemExit(0 if result['passed']==result['total'] else 1)
