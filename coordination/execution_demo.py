"""Local simulation lab for the delegated execution contract. No actuator API.

Run: python3 -m coordination.execution_demo --port 8772
Time advances only through explicit test actions; opening this page never moves
vehicles. Each reset creates a new isolated trial, not a checkpoint replan.
"""
from copy import deepcopy
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import threading
import uuid

from coordination.errors import ApiError
from coordination.execution import ExecutionLedger,canonical,expected
from simulator.delegated_execution import VirtualVehicle
from simulator.execution_trajectory import ValidatedTrajectory
from simulator import curb_parking as cp


class ExecutionLab:
    def __init__(self,root,trajectory=None):
        self.root=Path(root);self.trajectory=trajectory or ValidatedTrajectory(cp.demo_scene())
        self.server_epoch=str(uuid.uuid4());self.revision=0;self.receipts={};self.lock=threading.Lock();self.reset()

    def reset(self):
        self.demo_id=str(uuid.uuid4());self.folder=self.root/self.demo_id;self.now=1_000_000
        self.ledger=ExecutionLedger(self.folder/'authority.db',lambda:self.now);self.vehicles={};self.bundle=None;self.auto_coverage=True
        source=self.trajectory.as_dict()
        for v in sorted({c['vehicle'] for c in source['commands']}):
            token=self.ledger.register_agent(v,'register_'+v)['token']
            cost=sum(abs(c['distance']) for c in source['commands'] if c['vehicle']==v)
            self.ledger.delegate(v,'SIM-OWNER','SIM-CURB',self.now,self.now+600000,cost+1,cost*2+1,100,100,100,'delegate_'+v)
            self.vehicles[v]=VirtualVehicle(self.folder/(v+'.db'),v,token,self.ledger,source['initial'][v])
        self.pid=self.ledger.propose(self.trajectory,'SIM-CURB',self.now+300000,'propose',require_traffic=True)['planId']
        self.observe([]);self.now+=1000;self.observe([])
        for v in self.vehicles.values():v.ready(self.pid)

    def observe(self,tracks):
        p=self.ledger.state(self.pid);t=p.get('traffic')
        return self.ledger.observe_traffic(self.pid,t['revision'] if t else 0,self.now,tracks,str(uuid.uuid4()))

    def snapshot(self):
        state=self.ledger.state();plan=state['plans'][self.pid]
        c=plan['commands'][plan['index']] if plan['index']<len(plan['commands']) else None
        path=[] if c is None or plan['status'] not in ('ready','running','stop_requested') or c['status'] not in ('pending','dispatched','accepted','executing') else [expected(c,abs(c['distance'])*i/40) for i in range(41)]
        return dict(simulationOnly=True,serverEpoch=self.server_epoch,demoId=self.demo_id,revision=self.revision,
            virtualTimeMillis=self.now,plan=plan,poses=state['poses'],usage=state['usage'],grants=state['grants'],
            events=state['events'][-60:],path=path,autoCoverage=self.auto_coverage)

    def action(self,payload):
        if not isinstance(payload,dict):raise ApiError(400,'invalid_lab_action')
        if set(payload)!={'action','requestId','serverEpoch','demoId','revision'}:raise ApiError(400,'invalid_lab_action')
        action=payload.get('action');request=payload.get('requestId')
        try:
            if str(uuid.UUID(request))!=request:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ApiError(400,'invalid_request_id') from None
        if action not in ('reset','next','risk','stale','clear','confirm-stop'):raise ApiError(400,'invalid_lab_action')
        try:signature=hashlib.sha256(canonical(payload).encode()).hexdigest()
        except (TypeError,ValueError,OverflowError):raise ApiError(400,'invalid_lab_action') from None
        with self.lock:
            if payload.get('serverEpoch')!=self.server_epoch:raise ApiError(409,'lab_instance_changed')
            previous=self.receipts.get(request)
            if previous:
                if previous!=signature:raise ApiError(409,'request_id_reused')
                return dict(self.snapshot(),receiptReplayed=True)
            if payload.get('demoId')!=self.demo_id or type(payload.get('revision'))!=int or payload['revision']!=self.revision:raise ApiError(409,'lab_view_changed')
            p=self.ledger.state(self.pid)
            if action=='next' and p['status'] not in ('ready','running'):raise ApiError(409,'lab_execution_not_running')
            if action=='confirm-stop' and (p['status']!='stop_requested' or self.bundle is None):raise ApiError(409,'no_stop_confirmation_pending')
            if action=='reset':self.reset()
            else:
                self.now+=50
                if action=='stale':
                    self.auto_coverage=False;self.now+=1501
                elif action=='risk':
                    self.auto_coverage=False
                    p=self.ledger.state(self.pid);c=p['commands'][p['index']] if p['index']<len(p['commands']) else p['commands'][0]
                    body=self.ledger.state()['poses'][c['vehicle']]
                    self.observe([dict(id='SIM-PED',kind='pedestrian',pose=dict(x=body['x'],y=6.5,yaw=0),length=.5,width=.5,vx=0,vy=-.8,observedAtMillis=self.now,durationSeconds=0)])
                elif action=='clear':
                    self.observe([]);self.now+=1000;self.observe([]);self.auto_coverage=True
                elif action=='confirm-stop':
                    if self.ledger.state(self.pid)['status']!='stop_requested' or self.bundle is None:raise ApiError(409,'no_stop_confirmation_pending')
                    self.vehicles[self.bundle['command']['vehicle']].confirm_stop(self.bundle);self.bundle=None
                elif action=='next':
                    p=self.ledger.state(self.pid)
                    if p['status'] not in ('ready','running'):raise ApiError(409,'lab_execution_not_running')
                    if self.auto_coverage:self.observe([])
                    for v in self.vehicles.values():v.heartbeat()
                    if p['status']=='ready':self.ledger.start(self.pid,str(uuid.uuid4()))
                    p=self.ledger.state(self.pid);c=p['commands'][p['index']]
                    if c['status']=='pending':
                        self.bundle=self.ledger.dispatch(self.pid,str(uuid.uuid4()));c=self.bundle['command'];agent=self.vehicles[c['vehicle']]
                        self.ledger.accept(self.pid,agent.vehicle,agent.token,self.bundle['planDigest'],self.bundle['grantVersion'],c['id'],c['sequence'],'accept_'+c['id'])
                        agent._report(self.bundle,'executing',1,0.0,c['source'],'moving_'+c['id'])
                    elif c['status']=='executing' and self.bundle:
                        self.vehicles[c['vehicle']].execute(self.bundle);self.bundle=None
                    else:raise ApiError(409,'lab_command_unconfirmed')
                # Heartbeats are not observations and cannot clear stale coverage.
                for v in self.vehicles.values():v.heartbeat()
            self.revision+=1;self.receipts[request]=signature
            result=self.snapshot();(self.folder/'snapshot.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            return result


class LabServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,lab):self.lab=lab;super().__init__(address,LabHandler)


class LabHandler(BaseHTTPRequestHandler):
    def guard(self):
        valid=(f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
        if self.headers.get('Host') not in valid:raise ApiError(403,'local_lab_only')
        if self.headers.get('Origin') and self.headers['Origin'] not in tuple('http://'+h for h in valid):raise ApiError(403,'lab_origin_rejected')

    def reply(self,status,raw,content_type='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers();self.wfile.write(raw)

    def handle_request(self,post=False):
        try:
            self.connection.settimeout(3)
            self.guard();path=self.path.split('?',1)[0]
            if not post and path in ('/','/execution-lab.css','/execution-lab.js'):
                file={'/':'execution-lab.html','/execution-lab.css':'execution-lab.css','/execution-lab.js':'execution-lab.js'}[path]
                mime='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'text/javascript'
                self.reply(200,(Path(__file__).parent/'admin'/file).read_bytes(),mime+'; charset=utf-8');return
            if not post and path=='/api/state':
                with self.server.lab.lock:result=self.server.lab.snapshot()
            elif post and path=='/api/actions':
                if self.headers.get_content_type()!='application/json':raise ApiError(415,'json_required')
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:raise ApiError(400,'invalid_body') from None
                if not 0<size<=2048:raise ApiError(413,'invalid_body_size')
                try:payload=json.loads(self.rfile.read(size),parse_constant=lambda v:(_ for _ in ()).throw(ValueError('invalid number')))
                except (ValueError,UnicodeDecodeError):raise ApiError(400,'invalid_json') from None
                result=self.server.lab.action(payload)
            else:raise ApiError(404,'not_found')
            self.reply(200,json.dumps(result,ensure_ascii=False,allow_nan=False).encode())
        except ApiError as e:self.reply(e.status,json.dumps(dict(error=e.code)).encode())
        except (BrokenPipeError,ConnectionError,TimeoutError):return

    def do_GET(self):self.handle_request()
    def do_POST(self):self.handle_request(post=True)


def export_scenarios(output):
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        lab=ExecutionLab(folder)
        def act(action):
            s=lab.snapshot()
            return lab.action(dict(action=action,requestId=str(uuid.uuid4()),serverEpoch=s['serverEpoch'],demoId=s['demoId'],revision=s['revision']))
        states=dict(ready=lab.snapshot(),unconfirmed=act('next'),risk=act('risk'),cleared=act('clear'),stopped=act('confirm-stop'))
        act('reset');act('next');states['stale']=act('stale');act('reset')
        s=lab.snapshot()
        while s['plan']['status'] in ('ready','running'):s=act('next')
        states['completed']=s
        assert s['plan']['status']=='completed'
        path=Path(output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(states,ensure_ascii=False,indent=2)+'\n')
        return states


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8772);parser.add_argument('--root',default='work/execution-lab');parser.add_argument('--export-tests');args=parser.parse_args()
    if args.export_tests:
        states=export_scenarios(args.export_tests);print(len(states),'simulation states exported')
    else:
        server=LabServer(('127.0.0.1',args.port),ExecutionLab(args.root));print(f'가상 실행 시험실 http://127.0.0.1:{server.server_port}',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()
