# CAN bus captures, 8 September 2026

Two candump-style captures from the CAN bus monitor on one system's servo bus
(`can0`). Five servo drives are present as CANopen nodes 1, 2, 3, 5 and 6
(matching motors 1, 2, 3, 5, 6 in Grafana), plus the master (heartbeat on
COB-ID 700, alive tick on 27F). Source files: `18h.txt` and `19h.txt` in
Chris's Downloads. Interactive timeline for the first capture:
<https://claude.ai/code/artifact/49aade63-e9de-4590-bde7-09bab622f4f0>

## Capture 1: 18:32:52 to 18:35:58 (`18h.txt`)

155,741 frames in 3 minutes 6 seconds. Records a full power dip on the drives
and the recovery afterwards.

| Time | Event |
|---|---|
| 18:32:52 | Capture starts. Drives 1, 2, 5, 6 are in pre-operational, the master is operational. |
| 18:32:55.05 | Drives 1, 2, 3 and 6 raise emergency 0xFF80 (manufacturer error) within 2 ms of each other, then 0xFF00. |
| 18:32:55.16 | Error-frame storm begins: 17,691 error frames in one second. |
| 18:32:55 to 18:33:46 | 56,696 error frames in total, with dead gaps of 7.4 s and 6.2 s where nothing gets through. |
| 18:33:36 to 18:33:39 | All drives drop to state 02, then send boot-up messages: they have reset. |
| 18:33:42 | All five drives raise emergency 0x3120, CANopen "input voltage too low". The value bytes decode to about 35.0, most likely the supply voltage. |
| 18:33:48 to 18:33:50 | Drives drop to state 02 and boot up a second time. |
| 18:34:19 | All five drives reach operational. |
| 18:34:42 | The master resumes SYNC. Normal SDO polling continues to the end. |

Reading the error frames: the 56,696 frames on ID 1FFFFFFF carry a single
byte, almost always 07, 03 or 08. If the monitor writes the SocketCAN error
class there, those are transmit timeout + lost arbitration + controller
error, and protocol violation: a bus that is electrically unhappy
(transceivers browning out, drives resetting mid-frame), not a software
problem.

Link to the Elastic logs: the under-voltage emergency is the same fault the
actuator controller logs as "DS401: Input voltage too low", and the double
reset explains the "Actuators re-connected" and "Error Reset or No Error ::
N/A" lines that follow each wave (see capture 2 for what N/A is).

Not decoded: the 0xFF80 emergencies carry manufacturer bytes 26 / 46 / 47 /
48 that need the drive manual; the SDO objects 0x2014:01 (16-byte segmented
read from every drive roughly every 10 ms) and 0x2202:02 are also
manufacturer-specific.

The monitor's own tagging agrees: every frame up to 18:33:57 is NEAR_POWER,
everything after is OTHER.

### The 27F message

A 4-byte message from the master every 50 ms. The bytes are a little-endian
32-bit counter in microseconds that advances at one million per second and
wraps at 600 s. In CANopen terms 0x27F is RPDO1 for node 127, the address
conventionally used by the master, so it is the controller's alive tick with
its own clock inside. Two things in the stream:

- During the error storm no 27F got through for 40 s, yet the counter in
  the next one had only moved 1.1 s: that frame sat in the controller's
  transmit queue while the bus was down and everything behind it was
  dropped.
- After recovery (from 18:34:25) the tick pauses for 2 to 6 s roughly every
  10 s while drive SDO traffic continues. Capture 2 shows this is permanent.

## Capture 2: 18:59:49 to 19:13:42 (`19h.txt`)

807,796 frames in 13 minutes 52 seconds, no power event tagged, only 2 error
frames in the whole capture. This is the healthy running pattern plus one
deliberate stop/start of the drives.

**Traffic pattern.** About 1,000 frames/s of SDO traffic: reads of
0x2014:01 (status block) and 0x2202:02/03, and 42,901 segmented writes to
0x2200:02, which is the trajectory (PVT) streaming. The bus is silent for
0.5 to 1.0 s about 56 times every minute; 586 s of the 832 s are silence. The
master's heartbeat (250 ms nominal) and the 27F tick both stall for 3.5 to
4.5 s roughly every 10 s, 100 times in the capture, while SDO traffic keeps
flowing. So the master's CANopen housekeeping thread stalls regularly even
though its SDO work does not.

**The 19:05 stop/start.**

| Time | Event |
|---|---|
| 19:05:32.997 to 19:05:34.995 | Master sends NMT "enter pre-operational" to nodes 2, 5, 6; all five drives go pre-operational by 19:05:36. |
| 19:05:36.24 to 19:05:42.19 | Master heartbeat stalls for 5.9 s, the longest in the capture. |
| 19:05:38.00 | All five drives raise emergency 0x8130 (heartbeat / life-guard error), register 0x10 (communication), value 1501, i.e. 1.5 s without the master's heartbeat. |
| 19:05:42.19 | Master heartbeat returns; in the same millisecond all five drives send emergency 0x0000 "error reset / no error". |
| 19:06:05.67 | Master sends NMT "start" to node 5; all five drives reach operational by 19:06:10. Node 5 emits its TPDO2/3/4 once on entering operational. |

**What "Error Reset or No Error :: N/A" is.** It is the drives' emergency
0x0000, sent when a previous error clears. In the logs it therefore marks the
end of a fault (here the heartbeat loss), not a fault in itself. The
question to take to the drive vendor is why the master's heartbeat and tick
stall for several seconds every 10 s, since a stall over 1.5 s while the
drives are guarding the master is enough to trip 0x8130.
