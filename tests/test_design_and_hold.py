"""Public graph structure and neural-to-body translation, not neural simulation."""
import ast
import json
from pathlib import Path
import pytest
from aerohand_control.constants import MOTOR_KEYS,FINGERS,FINGER_MOTORS,input_contract,output_contract
from aerohand_control.design import design_dict,audit,design_summary
from aerohand_control.body_commands import motor_hold_mask,held_targets_and_deltas
from aerohand_control.neural import make_readings
ROOT=Path(__file__).resolve().parents[1]


def test_export_and_literal_python_semantically_equal():
    d=design_dict(); audit(d)
    assert json.loads((ROOT/'brain.abdesign.json').read_text())==d
    nodes=[]; edges=[]
    for stmt in ast.parse((ROOT/'brain_source.py').read_text()).body:
        if not isinstance(stmt,ast.Expr) or not isinstance(stmt.value,ast.Call): continue
        call=stmt.value
        if not isinstance(call.func,ast.Attribute): continue
        op=call.func.attr; args=[ast.literal_eval(a) for a in call.args]
        kw={k.arg:ast.literal_eval(k.value) for k in call.keywords}
        if op=='opu': nodes.append(dict(name=args[0],kind=op,rhythm=args[1],tnorm=args[2]))
        elif op in ('input','output'): nodes.append(dict(name=args[0],kind=op,neurons=args[1]))
        elif op=='connect': edges.append(dict({'from':args[0],'to':args[1],'type':args[2] if len(args)>2 else 'exc'},**kw))
    assert nodes==[{k:v for k,v in n.items() if k not in ('x','y')} for n in d['nodes']]
    assert edges==d['connections']
    assert design_summary(d)=={'motor_primitives':14,'inputs':19,'outputs':19,'opus':109,'populations':147,'connections':236}

@pytest.mark.parametrize('finger',FINGERS)
def test_tactile_is_local_neural_veto_and_output_clear_not_extension(finger):
    d=design_dict(); edges=d['connections']
    targets={e['to'] for e in edges if e['from'] in (f'grip_{finger}_opposition',f'grip_{finger}_hold_relay') and e['type']=='inh'}
    allowed={f'{MOTOR_KEYS[i]}_flex{s}' for i in FINGER_MOTORS[finger] for s in ('','_theta2')}
    assert allowed<=targets
    assert not any('_extend' in t for t in targets)
    assert not any(MOTOR_KEYS[i]+'_flex' in targets for i in range(7) if i not in FINGER_MOTORS[finger])
    assert any(e['from']==f'grip_{finger}_reset' and e['to']==f'grip_{finger}_hold' and e['type']=='inh' for e in edges)

@pytest.mark.parametrize('key',MOTOR_KEYS)
def test_full_primitive_and_antagonist(key):
    d=design_dict(); pairs={(e['from'],e['to'],e['type']) for e in d['connections']}
    n={x['name']:x for x in d['nodes']}
    for side,opp in [('flex','extend'),('extend','flex')]:
        p=f'{key}_{side}'
        assert n[p+'_theta3']['tnorm']==3 and n[p+'_theta2']['tnorm']==2.01
        assert n[p+'_beta5']['tnorm']==5 and n[p+'_reset']['tnorm']==3
        for source,target,kind in [(p+'_theta3',p+'_theta2','exc'),(p+'_theta2',p+'_beta5','exc'),
            (p+'_reset',p+'_theta2','inh'),(p+'_reset',p,'inh'),(p+'_beta5',f'{key}_{opp}_theta2','inh'),
            (p+'_beta5',f'{key}_{opp}','inh')]: assert (source,target,kind) in pairs

@pytest.mark.parametrize('finger',FINGERS)
def test_neural_hold_only_cancels_closing_and_preserves_other_fingers(finger):
    mask=motor_hold_mask({f'grip_{finger}_hold':1})
    assert [i for i,h in enumerate(mask) if h]==list(FINGER_MOTORS[finger])
    targets=[25.]*7; measured=[20.]*7; deltas=[1.]*7
    anchor,delta=held_targets_and_deltas(targets,measured,deltas,mask)
    for i in range(7):
        assert anchor[i]==(20. if mask[i] else 25.)
        assert delta[i]==(0. if mask[i] else 1.)
    _,delta=held_targets_and_deltas(targets,measured,[-1.]*7,mask)
    assert delta==[-1.]*7
    anchor,_=held_targets_and_deltas([15.]*7,measured,[-1.]*7,mask)
    assert anchor==[15.]*7  # an existing opening target is not pulled closed


def test_no_hold_cannot_create_or_cancel_commands():
    assert motor_hold_mask({})==[False]*7
    assert held_targets_and_deltas([25.]*7,[20.]*7,[1.]*7,[False]*7)==([25.]*7,[1.]*7)


def test_separate_coordinates_and_raw_continuous_sensors_no_reference():
    r=make_readings([80.]*7,[20.]*7,[(0.,100.)]*7,[0.,.25,.5,.75,1.])
    assert set(r)==set(input_contract())
    for key in MOTOR_KEYS:
        assert r['goal_'+key]['signal']==.8
        assert r['position_'+key]['signal']==.2
    assert r['touch_thumb']=={'signal':[0.0]*64,'mode':'vector_f32'}
    assert [sum(r['touch_'+f]['signal'])/64 for f in FINGERS[1:]]==[.25,.5,.75,1.]
    assert all(r['touch_'+f]['mode']=='vector_f32' for f in FINGERS)
    assert 'GRIP_REFERENCE' not in r
    assert not any('ACK' in k or 'COMMAND'==k for k in r)
    assert set(output_contract())=={f'{k}_{s}' for k in MOTOR_KEYS for s in ('flex','extend')}|{f'grip_{f}_hold' for f in FINGERS}

@pytest.mark.parametrize('value',[True,float('nan'),-1,1.1])
def test_invalid_tactile_values_rejected(value):
    with pytest.raises(ValueError): make_readings([80.]*7,[20.]*7,[(0.,100.)]*7,[value]*5)


def test_no_pressure_reference_threshold_or_ack_populations():
    d=design_dict()
    assert len([n for n in d['nodes'] if n['kind']=='input'])==19
    for n in d['nodes']:
        assert not any(x in n['name'].lower() for x in ('reference','above','below','enough','ack'))


@pytest.mark.parametrize('finger',FINGERS)
def test_full_tactile_chain_has_local_reset_and_output_clear(finger):
    d=design_dict();nodes={n['name']:n for n in d['nodes']}
    edges={(e['from'],e['to']):e for e in d['connections']}
    p='touch_'+finger;opposition='grip_'+finger+'_opposition';reset='grip_'+finger+'_reset';held='grip_'+finger+'_hold'
    for name,rhythm,tnorm in [(p+'_theta3','theta',3),(p+'_theta2','theta',2.01),(opposition,'beta',5),(reset,'beta',3)]:
        assert nodes[name]['rhythm']==rhythm and nodes[name]['tnorm']==tnorm
    for a,b in [(p,p+'_theta3'),(p+'_theta3',p+'_theta2'),(p+'_theta2',opposition),(opposition,reset),(opposition,held)]:
        assert edges[a,b]['type']=='exc'
    assert edges[reset,p+'_theta2']['role']=='feedback'
    assert edges[reset,held]['type']=='inh' and 'role' not in edges[reset,held]


def test_every_population_has_positive_construction_path():
    d=design_dict();seen={n['name'] for n in d['nodes'] if n['kind']=='input'}
    changed=True
    while changed:
        old=len(seen)
        seen.update(e['to'] for e in d['connections'] if e['from'] in seen and e['type']=='exc' and e.get('role')!='feedback')
        changed=len(seen)!=old
    assert seen=={n['name'] for n in d['nodes']}


def test_common_source_rhythms_and_matched_signed_operands():
    d=design_dict();nodes={n['name']:n for n in d['nodes']}
    for n in d['nodes']:
        if n['kind']!='opu':continue
        incoming=[e for e in d['connections'] if e['to']==n['name'] and e.get('role')!='feedback']
        exciting={nodes[e['from']].get('rhythm') for e in incoming if e['type']=='exc' and nodes[e['from']]['kind']=='opu'}
        if n['rhythm']!='high-gamma':assert len(exciting)<=1
        operands=[e for e in incoming if e.get('role')=='operand']
        if operands:
            assert {nodes[e['from']]['rhythm'] for e in operands}=={n['rhythm']}
            assert all(e.get('strength',1)==1 and e['units']==1 for e in operands)
