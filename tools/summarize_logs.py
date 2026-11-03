#!/usr/bin/env python3
"""Summarize recorded motor translation, without claiming a neural diagnosis."""
from pathlib import Path
import argparse
from collections import Counter
import json

ROOT=Path(__file__).resolve().parents[1]
KEYS=('thumb_cmc_abd','thumb_cmc_flex','thumb_tendon','index_tendon','middle_tendon','ring_tendon','pinky_tendon')

def summarize(directory):
    directory=Path(directory)
    result={'run':str(directory),'dispositions':Counter(),'motors':{
        k:{'flex':0,'extend':0,'both_frames':0,'requested_abs_deg':0.,'applied_abs_deg':0.,'limits':Counter()} for k in KEYS}}
    for line in (directory/'motion.jsonl').read_text().splitlines():
        try:r=json.loads(line)['data']
        except (ValueError,KeyError):continue
        result['dispositions'][r.get('disposition','unknown')]+=1
        raw=r.get('raw',{})
        for i,k in enumerate(KEYS):
            m=result['motors'][k];f=raw.get(k+'_flex',0);e=raw.get(k+'_extend',0)
            m['flex']+=f;m['extend']+=e;m['both_frames']+=int(f>0 and e>0)
            if len(r.get('decoded_delta',[]))==7:m['requested_abs_deg']+=abs(r['decoded_delta'][i])
            if len(r.get('applied_delta',[]))==7:m['applied_abs_deg']+=abs(r['applied_delta'][i])
            if len(r.get('limit_reasons',[]))==7:m['limits'].update(r['limit_reasons'][i])
    result['note']='Counts include held/observed frames. Applied angles are target increments, not physical travel. Inspect dispositions and raw neuron data.'
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',nargs='?',type=Path);a=p.parse_args()
    try:
        directory=a.run or max((x for x in (ROOT/'logs').glob('run-*') if x.is_dir()),key=lambda x:x.stat().st_mtime)
        print(json.dumps(summarize(directory),indent=2))
    except (OSError,ValueError) as e:p.exit(1,str(e)+'\n')
