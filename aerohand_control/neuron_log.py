"""Read-only neuron telemetry recording from the public authenticated SSE endpoint.

This subscriber never supplies inputs, changes device state, resets dynamics, or
controls motors. A telemetry failure is logged, not used as a motion command.
Raw neuron/input bitsets and the device's layout are retained verbatim. Empty
frames remain present; absence of a telemetry field is marked, not invented.
"""
from __future__ import annotations
import json
import re
import threading
import time

CURSOR=re.compile(r'^[0-9a-f]{32}:[0-9]{1,20}$')

def sse_messages(lines):
    event='message'; identity=None; parts=[]; size=0
    for line in lines:
        if not isinstance(line,str):line=line.decode('utf-8')
        if line=='':
            if parts:
                yield event,identity,json.loads('\n'.join(parts))
            event='message';identity=None;parts=[];size=0
            continue
        if line.startswith(':'):continue
        key,sep,value=line.partition(':')
        if value.startswith(' '):value=value[1:]
        if key=='event':event=value
        elif key=='id':identity=value
        elif key=='data':
            size+=len(value)
            if size>4*1024*1024:raise ValueError('Telemetry event exceeded 4 MiB')
            parts.append(value)

class NeuronRecorder:
    def __init__(self,url,api_key,recorder,*,client_factory=None):
        self.url=url;self._key=api_key;self.recorder=recorder;self.client_factory=client_factory
        self._stop=threading.Event();self.thread=None;self.cursor='';self.frames=0
        self.state='not_started';self.error=None;self.last_epoch=None;self.last_sequence=None
        self._response=None;self.reconnections=0

    def status(self):
        return {'status':self.state,'error':self.error,'frames':self.frames,'reconnections':self.reconnections}

    def start(self):
        self.thread=threading.Thread(target=self._run,daemon=True,name='AeroHandNeuronLog');self.thread.start()

    def _event(self,kind,cursor,data):
        if kind=='hello':
            self.recorder.record('neurons','layout',data)
            if data.get('error'):
                self.error=str(data['error']);self.state='unavailable'
            else:self.state='recording';self.error=None
        elif kind=='frame':
            epoch,seq=data.get('epoch'),data.get('sequence')
            if type(seq) is not int or seq<0 or not isinstance(epoch,str):
                raise ValueError('Invalid telemetry frame identity')
            if self.last_epoch is not None:
                if epoch!=self.last_epoch:
                    self.recorder.record('neurons','epoch_change',data);self.state='run_changed';return False
                if seq<=self.last_sequence:return True
                if seq!=self.last_sequence+1:
                    self.recorder.record('neurons','sequence_gap',{'previous':self.last_sequence,'current':seq})
            self.last_epoch,self.last_sequence=epoch,seq
            if cursor and CURSOR.fullmatch(cursor):self.cursor=cursor
            self.recorder.record('neurons','frame',data);self.frames+=1
            if data.get('telemetry') is None:
                self.state='waiting_for_neurons'
            elif data['telemetry'].get('error'):
                self.error=data['telemetry']['error'];self.state='unavailable'
            else:self.state='recording';self.error=None
        elif kind in ('gap','reset','error'):
            self.recorder.record('neurons',kind,data);self.error=str(data);self.state=kind
            return False
        else:self.recorder.record('neurons',kind,data)
        return True

    def _run(self):
        import httpx
        factory=self.client_factory or httpx.Client
        self.state='connecting'
        while not self._stop.is_set() and not self.recorder.full and not self.recorder.error:
            try:
                with factory(base_url=self.url,headers={'Authorization':'Bearer '+self._key},
                             timeout=httpx.Timeout(10.0,read=6.0),trust_env=False) as client:
                    params={'telemetry':'true','replay':'false'}
                    if self.cursor:params['cursor']=self.cursor
                    with client.stream('GET','/api/brain/stream',params=params) as response:
                        self._response=response
                        if response.status_code in (401,403,404,409):
                            self.error=f'Neuron stream HTTP {response.status_code}; output/control logs continue'
                            self.state='unavailable';self.recorder.record('neurons','unavailable',{'error':self.error});return
                        response.raise_for_status()
                        if 'text/event-stream' not in response.headers.get('content-type',''):
                            raise ValueError('Neuron stream is not SSE')
                        for kind,cursor,data in sse_messages(response.iter_lines()):
                            if self._stop.is_set() or self.recorder.full or self.recorder.error:return
                            if not self._event(kind,cursor,data):return
                        if not self._stop.is_set():raise IOError('Neuron stream ended')
            except Exception as exc:
                if self._stop.is_set():break
                self.error=f'{type(exc).__name__}: {exc}';self.state='reconnecting';self.reconnections+=1
                self.recorder.record('neurons','reconnect',{'error':self.error,'cursor':self.cursor})
                self._stop.wait(1.0)
            finally:self._response=None
        if self._stop.is_set():self.state='stopped'

    def close(self):
        self._stop.set()
        if self._response is not None:
            try:self._response.close()
            except Exception:pass
        if self.thread is not None:self.thread.join(timeout=7)
        if self.thread and self.thread.is_alive():self.state='close_timeout'
        elif self.state in ('recording','connecting','reconnecting','waiting_for_neurons'):self.state='stopped'
