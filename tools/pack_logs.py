#!/usr/bin/env python3
"""Zip the latest completed run. Includes diagnostic files, never .env files."""
from pathlib import Path
import argparse
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def pack(directory=None,output=None):
    if directory is None:
        choices=[p for p in (ROOT/'logs').glob('run-*') if p.is_dir() and (p/'run.json').is_file()]
        if not choices:raise ValueError('No recorded sessions found in logs/')
        directory=max(choices,key=lambda p:p.stat().st_mtime)
    directory=Path(directory).resolve()
    if not (directory/'run.json').is_file():raise ValueError('Select a session folder containing run.json')
    output=Path(output).resolve() if output else directory.parent/(directory.name+'.zip')
    allowed={'run.json','brain.abdesign.json','poses-at-start.json','baseline-at-start.json',
             'events.jsonl','sensors.jsonl','outputs.jsonl','motion.jsonl','neurons.jsonl'}
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(allowed):
            path=directory/name
            if path.is_file() and not path.is_symlink():archive.write(path,directory.name+'/'+name)
    return output

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path);parser.add_argument('--output',type=Path)
    a=parser.parse_args()
    try:print(pack(a.run,a.output))
    except (OSError,ValueError) as exc:parser.exit(1,str(exc)+'\n')
