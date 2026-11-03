"""Public neural I/O and physical sensor mapping."""
MOTOR_KEYS = ("thumb_cmc_abd", "thumb_cmc_flex", "thumb_tendon",
              "index_tendon", "middle_tendon", "ring_tendon", "pinky_tendon")
FINGERS = ("thumb", "index", "middle", "ring", "little")
FINGER_MOTORS = {"thumb": (0, 1, 2), "index": (3,), "middle": (4,), "ring": (5,), "little": (6,)}
INPUT_NEURONS = 64
TACTILE_NEURONS = 64
OUTPUT_NEURONS = 1
VERSION = "0.0.0"
BRAIN_VERSION = "0.0.0"
GRIP_OUTPUTS = tuple(f"grip_{finger}_hold" for finger in FINGERS)
# Only output decoding uses this alias, never a Python pressure threshold.
DIAGNOSTIC_OUTPUTS = GRIP_OUTPUTS

def input_contract():
    d = {f"{kind}_{key}": INPUT_NEURONS for key in MOTOR_KEYS for kind in ("goal", "position")}
    d.update({f"touch_{finger}": TACTILE_NEURONS for finger in FINGERS})
    return d

def output_contract():
    return {name: 1 for name in (*GRIP_OUTPUTS, *(f"{key}_{side}" for key in MOTOR_KEYS for side in ("flex", "extend")))}
