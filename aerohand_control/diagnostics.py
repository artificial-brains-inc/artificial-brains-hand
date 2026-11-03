"""Read-only counters for diagnosing quiet, opposed, held, and limited motors."""
from __future__ import annotations
from collections import deque
import time
from .constants import MOTOR_KEYS, FINGER_MOTORS

class MotorDiagnostics:
    def __init__(self, clock=time.monotonic):
        self.clock=clock;self.reset()

    def reset(self):
        self.started=self.clock();self.times=deque();self.frames=0;self.applied_frames=0
        self.motors={key:{'flex_count':0,'extend_count':0,'both_active_frames':0,
                           'grip_hold_frames':0,'requested_abs_degrees':0.,'applied_abs_degrees':0.,
                           'limited_frames':0,'last_limits':[], 'last_disposition':'no_frame'} for key in MOTOR_KEYS}

    def observe(self, frame):
        self.frames+=1; self.times.append(frame.captured_at)
        for key,d in zip(MOTOR_KEYS,frame.deltas):
            item=self.motors[key];f=frame.raw.get(key+'_flex',0);e=frame.raw.get(key+'_extend',0)
            item['flex_count']+=f;item['extend_count']+=e
            item['both_active_frames']+=int(f>0 and e>0);item['requested_abs_degrees']+=abs(d)
        for finger,indices in FINGER_MOTORS.items():
            if frame.raw.get('grip_'+finger+'_hold',0):
                for i in indices:self.motors[MOTOR_KEYS[i]]['grip_hold_frames']+=1

    def disposition(self, reason, result=None):
        if result is not None:self.applied_frames+=1
        for i,key in enumerate(MOTOR_KEYS):
            m=self.motors[key];m['last_disposition']=reason
            if result is not None:
                applied=getattr(result,'applied_deltas',())
                limits=getattr(result,'limit_reasons',())
                if applied:m['applied_abs_degrees']+=abs(applied[i])
                current=list(limits[i]) if limits else []
                if current or (applied and applied[i]):m['last_limits']=current
                m['limited_frames']+=int(bool(current))

    def snapshot(self):
        now=self.clock()
        while self.times and self.times[0]<now-1:self.times.popleft()
        return {'scope':'since latest command selection','output_hz':len(self.times),
                'frames':self.frames,'applied_frames':self.applied_frames,
                'elapsed_s':max(0,now-self.started),'motors':{k:dict(v) for k,v in self.motors.items()}}
