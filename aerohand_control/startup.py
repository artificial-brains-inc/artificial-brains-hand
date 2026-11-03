"""Explicit body initialization, completed before creating the neural session.

Only this pre-session step sends a preset directly to the hand. Once the brain
starts, normal movements continue to come from neural FLEX/EXTEND/HOLD outputs.
Observation/read-only modes never invoke this module's movement function.
"""
from __future__ import annotations
import math
import time
from typing import Callable


class StartupCancelled(RuntimeError):
    """An operator stopped or held the hand before neural control started."""


def open_before_brain(hand, goal, *, timeout_s: float = 30.0,
                      tolerance_degrees: float = 2.0, settle_s: float = 0.15,
                      poll_s: float = 0.05, cancelled: Callable[[], bool] = lambda: False,
                      progress: Callable[[dict], None] = lambda data: None,
                      confirm_indices=(0, 1, 2, 3, 4, 5, 6),
                      clock=time.monotonic, sleep=time.sleep) -> list[float]:
    """Send OPEN once, then confirm the selected measured axes at their targets.

    Live control selects indices 1..6. Abduction is an explicitly manual axis;
    its measured value remains visible but never gates neural session startup.

    No homing, reset, or speed/torque write is performed. The adapter applies
    the same logical-to-hardware polarity transform as normal movement. A
    missing feedback packet is recoverable. Neither a timer nor a successful
    serial write is treated as proof that the hand has physically opened.
    """
    from .hand import HandReadTimeout
    from .constants import MOTOR_KEYS
    if len(goal) != 7 or any(type(v) not in (int, float) or not math.isfinite(v) for v in goal):
        raise ValueError('Startup open target requires seven finite actuator coordinates')
    for value, label in ((timeout_s,'timeout'), (tolerance_degrees,'tolerance'),
                         (settle_s,'settle duration'), (poll_s,'poll interval')):
        if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Startup {label} must be finite and positive')
    confirm_indices = tuple(confirm_indices)
    if not confirm_indices or any(type(i) is not int or not 0 <= i < 7 for i in confirm_indices):
        raise ValueError("Startup confirmation indices must select motor axes")
    if cancelled():
        raise StartupCancelled('Opening cancelled; brain was not started.')
    goal = [float(v) for v in goal]
    # Expose both sides of the adapter transform. A logical zero on a reversed
    # axis is not a hardware zero. Diagnostic-only: this does not command motion.
    convert = getattr(hand, "hardware_coordinates", None)
    hardware_target = convert(goal) if callable(convert) else None
    started = clock(); stable_since = None; timeouts = 0; measured = None
    progress({'state':'opening','target':goal,'hardware_target':hardware_target,'positions':None,'elapsed_s':0.0,
              'confirmed':False,'feedback_timeouts':0})
    hand.command_startup_pose(goal)
    while clock() - started < timeout_s:
        if cancelled():
            raise StartupCancelled('Opening cancelled; brain was not started.')
        try:
            measured = hand.read_positions()
        except HandReadTimeout:
            timeouts += 1; stable_since = None
            progress({'state':'opening','target':goal,'hardware_target':hardware_target,'positions':None,
                      'elapsed_s':clock()-started,'confirmed':False,'feedback_timeouts':timeouts})
        else:
            errors = [abs(a-b) for a,b in zip(measured,goal)]
            within = len(measured)==7 and all(math.isfinite(v) for v in measured) and max(errors[i] for i in confirm_indices) <= tolerance_degrees
            if within:
                if stable_since is None: stable_since=clock()
            else:
                stable_since=None
            confirmed = stable_since is not None and clock()-stable_since >= settle_s
            progress({'state':'open' if confirmed else 'opening','target':goal,'hardware_target':hardware_target,
                      'positions':list(measured),'error_degrees':errors,
                      'confirmed_indices':list(confirm_indices),
                      'elapsed_s':clock()-started,'confirmed':confirmed,'feedback_timeouts':timeouts})
            if cancelled():
                raise StartupCancelled('Opening cancelled; brain was not started.')
            if confirmed:
                return list(measured)
        sleep(min(poll_s,max(0.0,timeout_s-(clock()-started))))
    remaining = ', '.join(f'{name}: {abs(actual-target):.1f} deg'
                          for i,(name,actual,target) in enumerate(zip(MOTOR_KEYS,measured or [],goal))
                          if i in confirm_indices and abs(actual-target)>tolerance_degrees)
    raise TimeoutError(f'Open position was not confirmed within {timeout_s:g}s; brain was not started. '
                       + (f'Remaining position error: {remaining}.' if remaining else 'Position feedback was incomplete.'))


def zero_before_brain(reader, encoding, baseline_path, *, cancelled=lambda: False,
                      progress=lambda data: None, clock=time.monotonic, sleep=time.sleep):
    """Capture unloaded offsets/noise after OPEN, once before any neural action.

    The operator must leave the opened fingertips free of contact. This is not
    object-specific force calibration and is never adapted during a grasp.
    """
    from .baseline import BaselineCapture, save_baseline
    capture=BaselineCapture(clock(),count=50,minimum_seconds=0.75,timeout_seconds=8.0)
    while True:
        if cancelled(): raise StartupCancelled('Unloaded zeroing cancelled; brain was not started.')
        sample=reader.latest(0.25)
        if capture.observe(sample,clock()):
            data=capture.result(encoding.directions)
            data['source']='Automatic unloaded capture after confirmed startup OPEN; fixed throughout this run'
            save_baseline(baseline_path,data)
            encoding.update_baseline(data['baseline'],data['noise_deadband'],source=data['source'])
            progress({'state':'zeroed','samples':len(capture.samples),'baseline':data['baseline'],
                      'noise_deadband':data['noise_deadband']})
            return data
        progress({'state':'zeroing','samples':len(capture.samples),'required':capture.count})
        sleep(0.01)
