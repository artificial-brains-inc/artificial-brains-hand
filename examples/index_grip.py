"""Generate the one-finger circuit explained in docs/neural-grip.md.

Run from the repository root with the installed SDK:
    python examples/index_grip.py
    ab design --brain examples/index-grip.abdesign.json

The full-hand application uses brain.abdesign.json, not this teaching design.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ab import Brain


def build_brain() -> Brain:
    brain = Brain("Index finger tactile grip")
    brain.input("goal_index_tendon", 64)
    brain.input("position_index_tendon", 64)
    brain.opu("goal_index_tendon_canonical", "theta", 2)
    brain.opu("position_index_tendon_canonical", "theta", 2)
    brain.opu("index_tendon_compare_flex", "theta", 2)
    brain.opu("index_tendon_compare_extend", "theta", 2)

    brain.connect("goal_index_tendon", "goal_index_tendon_canonical")
    brain.connect("position_index_tendon", "position_index_tendon_canonical")
    brain.connect("goal_index_tendon_canonical", "index_tendon_compare_flex",
                  role="operand", units=1)
    brain.connect("position_index_tendon_canonical", "index_tendon_compare_flex", "inh",
                  role="operand", units=1)
    brain.connect("position_index_tendon_canonical", "index_tendon_compare_extend",
                  role="operand", units=1)
    brain.connect("goal_index_tendon_canonical", "index_tendon_compare_extend", "inh",
                  role="operand", units=1)


    brain.opu("index_tendon_flex_theta3", "theta", 3)
    brain.opu("index_tendon_flex_theta2", "theta", 2.01)
    brain.opu("index_tendon_flex_beta5", "beta", 5)
    brain.opu("index_tendon_flex_reset", "beta", 3)
    brain.output("index_tendon_flex")
    brain.opu("index_tendon_extend_theta3", "theta", 3)
    brain.opu("index_tendon_extend_theta2", "theta", 2.01)
    brain.opu("index_tendon_extend_beta5", "beta", 5)
    brain.opu("index_tendon_extend_reset", "beta", 3)
    brain.output("index_tendon_extend")

    brain.connect("index_tendon_compare_flex", "index_tendon_flex_theta3")
    brain.connect("index_tendon_flex_theta3", "index_tendon_flex_theta2")
    brain.connect("index_tendon_flex_theta2", "index_tendon_flex_beta5")
    brain.connect("index_tendon_flex_beta5", "index_tendon_flex")
    brain.connect("index_tendon_flex_beta5", "index_tendon_flex_reset")
    brain.connect("index_tendon_flex_reset", "index_tendon_flex_theta2", "inh",
                  role="feedback", strength=1)
    brain.connect("index_tendon_flex_reset", "index_tendon_flex", "inh", strength=1)

    brain.connect("index_tendon_compare_extend", "index_tendon_extend_theta3")
    brain.connect("index_tendon_extend_theta3", "index_tendon_extend_theta2")
    brain.connect("index_tendon_extend_theta2", "index_tendon_extend_beta5")
    brain.connect("index_tendon_extend_beta5", "index_tendon_extend")
    brain.connect("index_tendon_extend_beta5", "index_tendon_extend_reset")
    brain.connect("index_tendon_extend_reset", "index_tendon_extend_theta2", "inh",
                  role="feedback", strength=1)
    brain.connect("index_tendon_extend_reset", "index_tendon_extend", "inh", strength=1)

    brain.connect("index_tendon_flex_beta5", "index_tendon_extend_theta2", "inh",
                  role="feedback", strength=1)
    brain.connect("index_tendon_flex_beta5", "index_tendon_extend", "inh", strength=1)
    brain.connect("index_tendon_extend_beta5", "index_tendon_flex_theta2", "inh",
                  role="feedback", strength=1)
    brain.connect("index_tendon_extend_beta5", "index_tendon_flex", "inh", strength=1)


    # Continue with the position comparisons and both motor paths above.
    brain.input("touch_index", 64)
    brain.opu("touch_index_theta3", "theta", 3)
    brain.opu("touch_index_theta2", "theta", 2.01)
    brain.opu("grip_index_opposition", "beta", 5)
    brain.opu("grip_index_reset", "beta", 3)
    brain.output("grip_index_hold")
    brain.opu("grip_index_hold_relay", "high-gamma", 10)

    brain.connect("touch_index", "touch_index_theta3")
    brain.connect("touch_index_theta3", "touch_index_theta2")
    brain.connect("touch_index_theta2", "grip_index_opposition")
    brain.connect("grip_index_opposition", "index_tendon_flex_theta2", "inh", strength=1)
    brain.connect("grip_index_opposition", "index_tendon_flex", "inh", strength=1)
    brain.connect("grip_index_opposition", "grip_index_hold")

    brain.connect("grip_index_opposition", "grip_index_reset")
    brain.connect("grip_index_reset", "touch_index_theta2", "inh", role="feedback", strength=1)
    brain.connect("grip_index_reset", "grip_index_hold", "inh", strength=1)
    brain.connect("grip_index_hold", "grip_index_hold_relay")
    brain.connect("grip_index_hold_relay", "index_tendon_flex_theta2", "inh",
                  role="feedback", strength=1)
    brain.connect("grip_index_hold_relay", "index_tendon_flex", "inh", strength=1)
    return brain


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().with_name("index-grip.abdesign.json"))
    args = parser.parse_args()
    brain = build_brain()
    warnings = brain.validate()
    if warnings:
        raise ValueError("SDK validation: " + "; ".join(str(item) for item in warnings))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    brain.save(str(args.output))
    print(f"Saved index-finger teaching design: {args.output}")
    print("For the full-hand application, keep using brain.abdesign.json at the project root.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
