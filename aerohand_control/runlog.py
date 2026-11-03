"""Bounded asynchronous observational logs. Never a motor-control dependency."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import secrets
import threading
import time

STREAMS = ('events', 'sensors', 'outputs', 'motion', 'neurons')
SECRET = re.compile(r'plx_[A-Za-z0-9_\-]+|Bearer\s+[^\s\"\']+', re.IGNORECASE)

def clean(value):
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if any(s in str(k).lower() for s in ('api_key','authorization','password','token'))
                         else clean(v)) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str): return SECRET.sub('[REDACTED]', value)
    if value is None or type(value) in (int,bool): return value
    if type(value) is float: return value if math.isfinite(value) else None
    return str(value)

class RunRecorder:
    def __init__(self, root: Path, *, metadata=None, max_mb=256, queue_size=4096):
        self.path=Path(root)/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3))
        self.path.mkdir(parents=True,exist_ok=False)
        self.max_bytes=int(max_mb*1024*1024); self.bytes_written=0
        self.queue=queue.Queue(maxsize=queue_size); self.dropped=Counter(); self.written=Counter()
        self.error=None; self.full=False; self.closed=False; self._stop=threading.Event()
        self.metadata=clean(metadata or {}); self.metadata['started_utc']=datetime.now(timezone.utc).isoformat()
        self._write_summary(False)
        self.thread=threading.Thread(target=self._worker,daemon=True,name='AeroHandLogWriter');self.thread.start()

    def record(self, stream: str, kind: str, data=None, **fields):
        if stream not in STREAMS: raise ValueError('Unknown log stream')
        if self.closed or self.full or self.error:
            self.dropped[stream]+=1;return
        item={'utc_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(),'kind':kind,
              'data':data if data is not None else fields}
        # Serialization/disk writes belong to the worker, never SDK output callbacks.
        try:self.queue.put_nowait((stream,item))
        except queue.Full:self.dropped[stream]+=1

    def status(self):
        return {'directory':str(self.path),'recording':not(self.closed or self.error or self.full),
                'bytes':self.bytes_written,'limit_bytes':self.max_bytes,'dropped':dict(self.dropped),
                'written':dict(self.written),'error':self.error,'size_limit_reached':self.full}

    def _write_summary(self, complete):
        payload={**self.metadata,'logging':self.status(),'closed_cleanly':complete,
                 'schema':'aerohand-run/v1','clock_note':'host monotonic/UTC plus device timestamps where supplied; not synchronized clocks'}
        tmp=self.path/'run.json.tmp';tmp.write_text(json.dumps(clean(payload),indent=2)+'\n');tmp.replace(self.path/'run.json')

    def _worker(self):
        handles={}; last_flush=time.monotonic()
        try:
            for name in STREAMS:handles[name]=(self.path/(name+'.jsonl')).open('w',encoding='utf-8')
            while not self._stop.is_set() or not self.queue.empty():
                try:stream,item=self.queue.get(timeout=.2)
                except queue.Empty:stream=None
                if stream is not None:
                    line=json.dumps(clean(item),separators=(',',':'),allow_nan=False)+'\n'
                    size=len(line.encode('utf-8'))
                    if self.bytes_written+size>self.max_bytes:self.full=True
                    if self.full:self.dropped[stream]+=1
                    else:
                        handles[stream].write(line);self.bytes_written+=size;self.written[stream]+=1
                    self.queue.task_done()
                if time.monotonic()-last_flush>=1:
                    for f in handles.values():f.flush()
                    self._write_summary(False);last_flush=time.monotonic()
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
        finally:
            for f in handles.values():
                try:f.close()
                except OSError:pass

    def close(self):
        if self.closed:return
        self.closed=True;self._stop.set();self.thread.join(timeout=5)
        if self.thread.is_alive():self.error='Log writer did not finish within five seconds'
        try:self._write_summary(not self.thread.is_alive() and self.error is None)
        except OSError as exc:self.error=f'{type(exc).__name__}: {exc}'


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
