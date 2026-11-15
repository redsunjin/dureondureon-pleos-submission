"""Validated curb trajectory + independently durable virtual vehicle agents.

Simulation only: the agent journal atomically changes a virtual pose once per
command. This is not a substitute for a real actuator's execution/stop protocol.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import uuid

from coordination.execution import ExecutionLedger, canonical, close_pose, digest
from simulator import curb_parking as cp


from simulator.execution_trajectory import ValidatedTrajectory


class VirtualVehicle:
    def __init__(self,path,vehicle,token,ledger,initial_pose):
        self.path=str(path);self.vehicle=vehicle;self.token=token;self.ledger=ledger
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS virtual_vehicle (vehicle TEXT PRIMARY KEY,pose TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS virtual_commands (id TEXT PRIMARY KEY,vehicle TEXT NOT NULL,signature TEXT NOT NULL,travel REAL NOT NULL,target TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO virtual_vehicle VALUES(?,?)',(vehicle,canonical(initial_pose)));db.commit()

    def heartbeat(self):return self.ledger.heartbeat(self.vehicle,self.token,str(uuid.uuid4()))

    def ready(self,pid):
        p=self.ledger.state(pid);self.heartbeat()
        return self.ledger.ready(pid,self.vehicle,self.token,p['digest'],p['versions'][self.vehicle],str(uuid.uuid4()))

    def _report(self,bundle,kind,seq,travel,pose,request):
        c=bundle['command']
        return self.ledger.report(bundle['planId'],self.vehicle,self.token,bundle['planDigest'],bundle['grantVersion'],c['id'],c['sequence'],seq,kind,travel,pose,request)

    def execute(self,bundle,deliver_completion=True):
        c=bundle['command']
        if c['vehicle']!=self.vehicle:raise ValueError('wrong virtual vehicle')
        with closing(sqlite3.connect(self.path)) as db:
            done=db.execute('SELECT signature FROM virtual_commands WHERE id=?',(c['id'],)).fetchone()
        if done and done[0]!=digest(bundle):raise ValueError('command content changed')
        if not done:
            self.ledger.accept(bundle['planId'],self.vehicle,self.token,bundle['planDigest'],bundle['grantVersion'],c['id'],c['sequence'],'accept_'+c['id'])
            self._report(bundle,'executing',1,0.0,c['source'],'moving_'+c['id'])
            # A cached ACK proves an earlier receipt, never a fresh movement permit.
            current=self.ledger.state(bundle['planId'])
            active=current['commands'][current['index']] if current['index']<len(current['commands']) else None
            if current['status']!='running' or active is None or active['id']!=c['id'] or active['status']!='executing':
                # Another delivery may have committed this command while we were
                # checking authority. Reconcile its journal, never replay motion.
                with closing(sqlite3.connect(self.path)) as db:
                    committed=db.execute('SELECT signature FROM virtual_commands WHERE id=?',(c['id'],)).fetchone()
                if committed and committed[0]==digest(bundle):
                    if deliver_completion:return self._report(bundle,'completed',2,abs(c['distance']),c['target'],'done_'+c['id'])
                    return dict(completionDelivered=False,commandId=c['id'])
                raise ValueError('virtual execution is no longer permitted')
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('BEGIN IMMEDIATE')
                # Deduplication and the virtual pose change are one local transaction.
                row=db.execute('SELECT signature FROM virtual_commands WHERE id=?',(c['id'],)).fetchone()
                if row:
                    if row[0]!=digest(bundle):raise ValueError('command content changed')
                else:
                    pose=json.loads(db.execute('SELECT pose FROM virtual_vehicle WHERE vehicle=?',(self.vehicle,)).fetchone()[0])
                    if not close_pose(pose,c['source']):raise ValueError('virtual source pose changed')
                    db.execute('UPDATE virtual_vehicle SET pose=? WHERE vehicle=?',(canonical(c['target']),self.vehicle))
                    db.execute('INSERT INTO virtual_commands VALUES(?,?,?,?,?)',(c['id'],self.vehicle,digest(bundle),abs(c['distance']),canonical(c['target'])))
                db.commit()
        if deliver_completion:return self._report(bundle,'completed',2,abs(c['distance']),c['target'],'done_'+c['id'])
        return dict(completionDelivered=False,commandId=c['id'])

    def confirm_stop(self,bundle):
        c=bundle['command'];p=self.ledger.state(bundle['planId']);current=p['commands'][p['index']]
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT travel FROM virtual_commands WHERE id=? AND vehicle=?',(c['id'],self.vehicle)).fetchone()
            pose=json.loads(db.execute('SELECT pose FROM virtual_vehicle WHERE vehicle=?',(self.vehicle,)).fetchone()[0])
        event=current['eventSequence']+1
        return self._report(bundle,'stopped',event,row[0] if row else 0.0,pose,'stopped_'+c['id']+'_'+str(event))

    def local_state(self):
        with closing(sqlite3.connect(self.path)) as db:
            pose=json.loads(db.execute('SELECT pose FROM virtual_vehicle WHERE vehicle=?',(self.vehicle,)).fetchone()[0])
            count=db.execute('SELECT COUNT(*) FROM virtual_commands WHERE vehicle=?',(self.vehicle,)).fetchone()[0]
        return dict(pose=pose,executedCommands=count,simulationOnly=True)


def demo(output,scenario="complete"):
    """One complete curb maneuver whose progress is driven exclusively by agents."""
    import tempfile
    trajectory=ValidatedTrajectory(cp.demo_scene());source=trajectory.as_dict();now=[1_000_000]
    with tempfile.TemporaryDirectory() as folder:
        ledger=ExecutionLedger(Path(folder)/'authority.db',lambda:now[0]);vehicles={}
        actors={c['vehicle'] for c in source['commands']}
        for v in sorted(actors):
            token=ledger.register_agent(v,'register_'+v)['token']
            commands=[c for c in source['commands'] if c['vehicle']==v]
            cost=sum(abs(c['distance']) for c in commands)
            ledger.delegate(v,'SIM-OWNER','SIM-CURB',now[0],now[0]+600000,cost+1,cost*2+1,100,100,100,'delegate_'+v)
            vehicles[v]=VirtualVehicle(Path(folder)/(v+'.db'),v,token,ledger,source['initial'][v])
        pid=ledger.propose(trajectory,'SIM-CURB',now[0]+300000,'propose')['planId']
        for agent in vehicles.values():agent.ready(pid)
        ledger.start(pid,'start')
        unknown=None;blocked=None
        if scenario=='complete':
            for i in range(len(source['commands'])):
                for agent in vehicles.values():agent.heartbeat()
                bundle=ledger.dispatch(pid,f'dispatch_{i}');vehicles[bundle['command']['vehicle']].execute(bundle)
                now[0]+=100
        else:
            bundle=ledger.dispatch(pid,'dispatch_0');c=bundle['command'];agent=vehicles[c['vehicle']]
            if scenario=='restart':
                ledger.accept(pid,agent.vehicle,agent.token,bundle['planDigest'],bundle['grantVersion'],c['id'],c['sequence'],'accept_'+c['id'])
                agent._report(bundle,'executing',1,0.0,c['source'],'moving_'+c['id'])
                ledger=ExecutionLedger(Path(folder)/'authority.db',lambda:now[0])
                for v in vehicles.values():v.ledger=ledger
                try:agent.execute(bundle);raise AssertionError('stale receipt authorized movement')
                except ValueError:blocked=True
            else:
                agent.execute(bundle,deliver_completion=False)
                if scenario=='revoke':ledger.revoke(agent.vehicle,'SIM-OWNER','revoke')
                elif scenario=='lost-completion':
                    for _ in range(20):
                        now[0]+=1000
                        for v in vehicles.values():v.heartbeat()
                        if ledger.state(pid)['status']=='stop_requested':break
                else:raise ValueError('unknown scenario')
            p=ledger.state(pid)
            assert p['status']=='stop_requested' and p['motionKnowledge']=='unknown'
            unknown=dict(status=p['status'],reason=p['reason'],motionKnowledge=p['motionKnowledge'],usage=ledger.state()['usage'])
            if scenario=='revoke':
                agent.execute(bundle)  # Late completion is recorded, but cannot replace stop confirmation.
                assert ledger.state(pid)['status']=='stop_requested'
            agent.confirm_stop(bundle)
        result=ledger.state();p=result['plans'][pid]
        assert p['status']==('completed' if scenario=='complete' else 'stopped')
        assert all(u['reserved']==0 for u in result['usage'].values())
        if scenario=='complete':assert all(close_pose(result['poses'][v],target) for v,target in source['finalPoses'].items())
        summary=dict(simulationOnly=True,scenario=scenario,unconfirmedBeforeStop=unknown,staleReceiptActuationBlocked=blocked,status=p['status'],completionSource='explicit_virtual_vehicle_reports',
            commandCount=len(p['commands']),eventCount=len(result['events']),usage=result['usage'],
            finalPoses=result['poses'],virtualExecutions=sum(a.local_state()['executedCommands'] for a in vehicles.values()),
            events=result['events'])
        Path(output).parent.mkdir(parents=True,exist_ok=True);Path(output).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        return summary

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='output/delegated-execution/demo.json');parser.add_argument('--scenario',choices=('complete','lost-completion','revoke','restart'),default='complete');args=parser.parse_args()
    r=demo(args.output,args.scenario);print(args.scenario,r['status'],r['virtualExecutions'],'virtual command executions')
