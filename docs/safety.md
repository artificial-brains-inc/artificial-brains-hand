# Bench setup and operation

Start with a powered, configured hand and a clear workspace. Follow the hand and
sensor manufacturers' electrical and mechanical instructions. Mount the touch
pads securely and route their cables away from tendons and moving joints.

## Before starting

Keep the hand empty and all five pads unloaded. Live startup moves the six active
motors to the Open posture and then captures the sensor baseline. Thumb abduction
stays at its measured or saved setup angle.

Begin experiments with bench objects and keep hands and body parts outside the
closing mechanism. Tactile opposition regulates closing activity; it is not a
verified human-contact force limit. Know how to disconnect actuator power
independently of the application.

## During a trial

Use **Hold** to request the measured hand position. A position hold leaves the
actuators powered; it is not a torque-disable or emergency-stop command. Loss of
serial communication can prevent a software command from reaching the hand.

Start at gain 1 and observe motion before increasing the increments. Larger
increments can increase contact overshoot. Check sensor order, polarity, and
unloaded zero before interpreting the contact display. A sensor reading is a
relative observation, not a force in newtons or proof of object retention.

The adapter keeps actuator travel, target-lead, and coupled-joint limits separate
from the neural tactile response. Inspect both outputs and applied movement when
assessing a grasp.

## Finishing

Select **Stop brain** and wait for confirmation that neural dynamics were saved
before disconnecting Prima-Lux. Reset dynamics only when you intend to discard
that state; resetting the brain does not reset the hand's physical posture.
