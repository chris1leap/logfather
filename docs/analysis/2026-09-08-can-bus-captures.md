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

Interactive timeline for 19:04:30 to 19:07:30 (the stop/start in the middle):
<https://claude.ai/code/artifact/df1c09fb-fefb-432a-a27b-e27b39524b19>

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

## Object 0x2202: the PVT trajectory buffer

Manufacturer object 0x2202 is the drive's queue of position-velocity-time
points streamed by the master (the segmented writes to 0x2200:02 are the
points themselves). Its three sub-indices, inferred from how they behave in
both captures:

| Sub-index | Access | Meaning |
|---|---|---|
| 2202:01 | written, always 0 | Clear the buffer. Written just before motion restarts (18:34:13; 19:05:44 to 19:06:06). |
| 2202:02 | polled continuously | Fill level: points waiting, 0 to about 52. Climbs while the master streams, falls one step every ~40 ms as the drive executes. |
| 2202:03 | read around a restart | Free space: 256 when empty, falling as points arrive. Fill plus free is 256, so the buffer holds 256 points. |

Values are little-endian 32-bit integers, so a reply of `00 01 00 00` means
256, an empty buffer. Every drive returned that at 19:06:06, one second after
the NMT start and just before the first points were loaded: the master
checking the buffers are clear before it begins streaming.

A 16-byte read of 0x2014:01 costs 8 frames (request, reply announcing 16
bytes, then three request/segment pairs of 7, 7 and 2 bytes), about one
millisecond of bus time per read; it runs about 48 times a second across the
five drives.

Also at 19:05:45 the master wrote configuration to every drive after the
reset: 0x2300:03 as a float of 3876 on drives 1 to 3 and 4560 on drives 5
and 6, and 0x4301:05 as 85 on drives 1 to 3 and 100 on drives 5 and 6.
Probably per-axis limits; the drive manual is needed to name them.

## Bus utilisation

The bit rate is 1 Mbit/s: the busiest 10 ms of the second capture holds 59
eight-byte frames, about 77% of a 1 Mbit/s bus, which no slower rate could
carry. Frame sizes assume standard 11-bit IDs, worst-case bit stuffing on the
stuffable part, and about 20 bits per error frame.

| | 18:32 capture | 18:59 capture |
|---|---|---|
| Averaged over the whole capture | 7.5% | 12.6% |
| Median second | 1.0% | 15.9% |
| Busiest second | 43.1% | 38.1% |
| While the bus is active (silences over 0.3 s excluded) | 28.6% | 42.5% |
| Busiest 10 ms | error storm, saturated | 76.7% |

So the bus is lightly loaded on average but the traffic is bursty: SDO
polling runs at 40 to 45% for a few hundred milliseconds, then the bus goes
quiet for most of a second.
