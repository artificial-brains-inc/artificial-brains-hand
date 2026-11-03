"""Coordinate reaching plus five independent tactile-opposition pathways.

Public design only: no construction formulas or private compiler implementation.
All arithmetic contributions are explicit signed operand units. The generated
JSON can be inspected without installing an SDK; live SDK validation is separate.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
from .constants import (MOTOR_KEYS, FINGERS, FINGER_MOTORS, INPUT_NEURONS,
                        TACTILE_NEURONS, BRAIN_VERSION, input_contract, output_contract)

VETO_STRENGTH = 1.0


def design_dict() -> dict:
    nodes, edges = [], []
    names = set()
    def node(name, kind, x, y, **params):
        if name in names:
            raise ValueError(f"Duplicate population {name}")
        names.add(name)
        nodes.append(dict(name=name, kind=kind, x=float(x), y=float(y), **params))
    def opu(name, rhythm, tnorm, x, y):
        node(name, "opu", x, y, rhythm=rhythm, tnorm=float(tnorm))
    def edge(source, target, kind="exc", **params):
        edges.append(dict({"from": source, "to": target, "type": kind}, **params))
    def operand(source, target, kind="exc", units=1.0):
        edge(source, target, kind, role="operand", units=float(units))
    def sensory(prefix, y):
        # Desired and measured positions get identical immediate Theta T2
        # representations before a separate Theta T2 arithmetic operator.
        node(prefix, "input", 120, y, neurons=INPUT_NEURONS)
        opu(prefix+"_canonical", "theta", 2, 1320, y)
        edge(prefix, prefix+"_canonical")
        return prefix+"_canonical"

    for i, key in enumerate(MOTOR_KEYS):
        y = 120 + 600*i
        goal = sensory(f"goal_{key}", y)
        position = sensory(f"position_{key}", y+240)
        for direction, positive, negative, yy in (("flex", goal, position, y),
                                                  ("extend", position, goal, y+240)):
            compare = f"{key}_compare_{direction}"
            opu(compare, "theta", 2, 1640, yy)
            operand(positive, compare)
            operand(negative, compare, "inh")
            prefix = f"{key}_{direction}"
            opu(prefix+"_theta3", "theta", 3, 1960, yy)
            opu(prefix+"_theta2", "theta", 2.01, 2260, yy)
            opu(prefix+"_beta5", "beta", 5, 2560, yy)
            opu(prefix+"_reset", "beta", 3, 2560, yy+110)
            node(prefix, "output", 2860, yy, neurons=1)
            edge(compare, prefix+"_theta3")
            edge(prefix+"_theta3", prefix+"_theta2")
            edge(prefix+"_theta2", prefix+"_beta5")
            edge(prefix+"_beta5", prefix)
            edge(prefix+"_beta5", prefix+"_reset")
            edge(prefix+"_reset", prefix+"_theta2", "inh", role="feedback", strength=1.0)
            edge(prefix+"_reset", prefix, "inh", strength=1.0)
        for side, opponent in (("flex", "extend"), ("extend", "flex")):
            edge(f"{key}_{side}_beta5", f"{key}_{opponent}_theta2", "inh", role="feedback", strength=1.0)
            edge(f"{key}_{side}_beta5", f"{key}_{opponent}", "inh", strength=1.0)

    # Five physical signals, not externally computed conditions or grip targets.
    # There is no captured pressure reference or above/below threshold circuit.
    # Each tactile pathway develops its own opposing state through neural dynamics.
    for i, finger in enumerate(FINGERS):
        y = 4500 + 600*i
        p = f"touch_{finger}"
        node(p, "input", 120, y, neurons=TACTILE_NEURONS)
        opu(p+"_theta3", "theta", 3, 420, y)
        opu(p+"_theta2", "theta", 2.01, 740, y)
        opposition = f"grip_{finger}_opposition"
        reset = f"grip_{finger}_reset"
        held = f"grip_{finger}_hold"
        relay = f"grip_{finger}_hold_relay"
        opu(opposition, "beta", 5, 1060, y)
        opu(reset, "beta", 3, 1060, y+160)
        node(held, "output", 1380, y, neurons=1)
        opu(relay, "high-gamma", 10, 1700, y)
        edge(p, p+"_theta3")
        edge(p+"_theta3", p+"_theta2")
        edge(p+"_theta2", opposition)
        edge(opposition, held)
        edge(opposition, reset)
        # Reconsider contact locally; do not leave a grip-HOLD latched forever
        # after the contact activity changes. Continuing touch can re-excite it.
        edge(reset, p+"_theta2", "inh", role="feedback", strength=1.0)
        edge(reset, held, "inh", strength=1.0)
        # A latched neural HOLD still inhibits closing between source spikes.
        # This is the relay's only excitatory drive, not a construction return.
        edge(held, relay)
        for idx in FINGER_MOTORS[finger]:
            key = MOTOR_KEYS[idx]
            edge(opposition, f"{key}_flex_theta2", "inh", strength=VETO_STRENGTH)
            edge(opposition, f"{key}_flex", "inh", strength=VETO_STRENGTH)
            edge(relay, f"{key}_flex_theta2", "inh", role="feedback", strength=VETO_STRENGTH)
            edge(relay, f"{key}_flex", "inh", strength=VETO_STRENGTH)
            # Nothing in the tactile path inhibits EXTEND. A held finger can open.
    return {"schema": "artificial-brains/design/v1", "name": f"Artificial Brains tactile hand v{BRAIN_VERSION}",
            "nodes": nodes, "connections": edges}


def audit(data: dict) -> None:
    nodes = {n['name']: n for n in data['nodes']}
    if len(nodes) != len(data['nodes']):
        raise ValueError("Duplicate populations")
    inputs = {n['name']: n['neurons'] for n in data['nodes'] if n['kind'] == 'input'}
    outputs = {n['name']: n['neurons'] for n in data['nodes'] if n['kind'] == 'output'}
    if inputs != input_contract() or outputs != output_contract():
        raise ValueError("Neural I/O contract mismatch")
    pairs = set()
    for e in data['connections']:
        s, t = e['from'], e['to']
        if s not in nodes or t not in nodes or (s, t) in pairs:
            raise ValueError("Unknown endpoint or duplicate connection")
        pairs.add((s, t))
        if e.get('role') == 'operand' and nodes[s]['rhythm'] != nodes[t]['rhythm']:
            raise ValueError("Arithmetic operands need canonical immediate sources")
        if e.get('role') and nodes[t]['kind'] != 'opu':
            raise ValueError("Connection roles require OPU targets")
    for key in MOTOR_KEYS:
        for side in ('flex', 'extend'):
            p = f'{key}_{side}'
            for s,t in [(p+'_theta3', p+'_theta2'), (p+'_theta2', p+'_beta5'),
                        (p+'_beta5', p), (p+'_beta5', p+'_reset'),
                        (p+'_reset', p+'_theta2'), (p+'_reset', p)]:
                if (s,t) not in pairs:
                    raise ValueError("Incomplete motor primitive")
    for e in data['connections']:
        if e['from'].startswith('grip_') and '_extend' in e['to']:
            raise ValueError("Tactile veto must not prevent opening")


def design_summary(brain=None) -> dict:
    data = design_dict() if brain is None else brain if isinstance(brain, dict) else brain.to_dict()
    counts = Counter(n['kind'] for n in data['nodes'])
    return {"motor_primitives": 14, "inputs": counts['input'], "outputs": counts['output'],
            "opus": counts['opu'], "populations": len(data['nodes']),
            "connections": len(data['connections'])}


def build_brain():
    from ab import Brain
    return Brain.from_dict(design_dict())


def export_design(directory: str | Path) -> dict:
    d = design_dict()
    audit(d)
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination/'brain.abdesign.json').write_text(json.dumps(d,indent=2)+'\n', encoding='utf-8')
    lines = ['# Generated public design. Build brain.abdesign.json after regenerating.',
             'from ab import Brain', '', f'brain = Brain({d["name"]!r})']
    for n in d['nodes']:
        if n['kind'] == 'opu':
            lines.append(f'brain.opu({n["name"]!r}, {n["rhythm"]!r}, {n["tnorm"]!r})')
        else:
            lines.append(f'brain.{n["kind"]}({n["name"]!r}, {n["neurons"]})')
    for e in d['connections']:
        args = [repr(e['from']), repr(e['to'])]
        if e['type'] == 'inh': args.append('"inh"')
        args.extend(f'{k}={e[k]!r}' for k in ('role','units','strength') if k in e)
        lines.append('brain.connect('+', '.join(args)+')')
    lines.append('brain.save("brain.abdesign.json")')
    (destination/'brain_source.py').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return design_summary(d)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1])
    print(json.dumps(export_design(p.parse_args().output),indent=2))
