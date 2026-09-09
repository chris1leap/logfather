# CAN bus captures, 8 September 2026

Two candump-style captures from the CAN bus monitor on one system's servo bus
(`can0`). Five servo drives are present as CANopen nodes 1, 2, 3, 5 and 6
(matching motors 1, 2, 3, 5, 6 in Grafana), plus the master (heartbeat on
COB-ID 700, alive tick on 27F). Source files: `18h.txt` and `19h.txt` in
Chris's Downloads. Interactive timeline for the first capture:
<https://claude.ai/code/artifact/49aade63-e9de-4590-bde7-09bab622f4f0>

## Abbreviations

| Term | Stands for | In these captures |
|---|---|---|
| COB-ID | Communication Object Identifier | The 11-bit CAN identifier: what a frame is and which node it concerns. |
| SDO | Service Data Object | Read or write one object-dictionary entry on request, "ask and answer". Requests on 601 to 606, replies on 581 to 586. Over 4 bytes it is chopped into 7-byte segments, so a 16-byte read costs 8 frames. |
| PDO | Process Data Object | Unrequested broadcast of live data, no address header, all 8 bytes payload. Defined on the drives (drive 5 sent TPDO2/3/4 once at 19:06:05) but not used in the running cycle. |
| NMT | Network Management | The master's start / stop / reset commands on COB-ID 000. |
| EMCY | Emergency | A node reporting an error, on 081 to 086. |
| SYNC | Synchronisation | The master's pulse on COB-ID 100, about every 18 s here. |
| PVT | Position Velocity Time | The trajectory points streamed into the drives' buffer (object 0x2202). |
| RPDO / TPDO | Receive / Transmit PDO | PDO seen from the node's side: it receives an RPDO, transmits a TPDO. 0x27F is RPDO1 for node 127, the master's alive tick. |

## Read this first: the monitor keeps about a third of the frames

The monitor writes a comment line every second with the number of frames it
saw (`total`) and the number of lines it wrote (`raw_lines_kept`). Over the
18:59 capture it saw 2,999,573 frames and wrote 807,796, about 27%. The kept
lines cluster in a window around each second boundary, so each second in the
file shows a burst of traffic then 0.5 to 0.7 s of apparent silence that is
not real. Every 10 to 11 s the tool keeps almost nothing for 2 to 4 s
(75 such runs in 14 minutes). These two artefacts were first read as bus
silences and as the master stalling; they are not. Corrected figures are in
the sections below; the timeline pages show the frames the tool kept.

What survives the correction: the 18:32 power dip and error storm (the 27F
counter proves the master's queue was frozen for 40 s), the drives'
heartbeat-loss emergency at 19:05:38 (the drives themselves reported it,
and the tool was keeping a third of frames through that stretch), and every
decoded format.

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
48 that need the drive manual. (0x2014:01 and 0x2202 are decoded in their
own sections below.)

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
- After recovery the tick appears to pause for 2 to 6 s roughly every 10 s,
  with its counter advancing normally across each pause: the frames were
  sent but not kept by the monitor (see "Read this first").

## Capture 2: 18:59:49 to 19:13:42 (`19h.txt`)

Interactive timeline for 19:04:30 to 19:07:30 (the stop/start in the middle):
<https://claude.ai/code/artifact/df1c09fb-fefb-432a-a27b-e27b39524b19>

807,796 frames in 13 minutes 52 seconds, no power event tagged, only 2 error
frames in the whole capture. This is the healthy running pattern plus one
deliberate stop/start of the drives.

**Traffic pattern.** The monitor's own totals show 3,500 to 4,300 frames
every second, continuously, while the system works: status reads of
0x2014:01 (about 56 reads a second per drive), PVT points into 0x2200:02
(about 40 a second per drive while moving), buffer polls of 0x2202:02, the
27F tick and the heartbeats. The kept lines show 1,000 to 1,500 of those
frames per second in a burst around each second boundary, which is why the
file looks bursty. Between 19:05:47 and 19:06:10, with the drives stopped,
the bus really is quiet: 29 to 31 frames a second, which is exactly the 27F
tick at 20/s, the master heartbeat at 4/s and five drive heartbeats at
about 1.1/s each.

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

## Motor temperature and current: the 0x2014:01 status block

There is no separate temperature or current message on the bus. Both ride
in the 16-byte status block the master reads from every drive through SDO
object 0x2014 sub-index 1. The block is four little-endian float32 values:

| Bytes | Field | Confirmed by |
|---|---|---|
| 0 to 3 | position, PVT units | tracks the PVT positions sent (drive 3: 0 to 102 on both) |
| 4 to 7 | velocity, PVT units/s | swings to about +/-250 only while moving, near 0 at rest |
| 8 to 11 | motor current, A | same range as Grafana `actuators_motor_current`; larger while moving (drive 1: 4.1 A moving, 0.5 A still, peak 10.7 A) |
| 12 to 15 | motor temperature, C | matches Grafana `actuators_motor_temperature` to within 0.02 C, correlation 1.00 on every drive |

The temperature match also confirms the captures came from PikPak 007
(35-2300-007).

How often: about 56 reads a second per drive, continuously while the
system works, one read of some drive every 3.5 ms on the bus (from the
monitor's totals and the 61% share of status frames; the kept lines alone
show bursts of four reads then a gap, which is the monitor's sampling, not
the master's). Each read costs 8 frames, so this is the largest single load
on the bus. Grafana keeps one sample every 30 s of what the master sees.

## PVT point format (object 0x2200:02)

Each trajectory point is a 12-byte segmented SDO write to 0x2200:02, three
little-endian 32-bit values:

| Bytes | Type | Meaning | Example |
|---|---|---|---|
| 0 to 3 | float32 | position | 32.51 |
| 4 to 7 | float32 | velocity | 103.04 |
| 8 to 11 | uint32 | time, ms | 20 |

Every one of the 42,901 points in the second capture has time = 20, so the
master streams a target every 20 ms of trajectory. The actuator controller
prints the same triple when a drive rejects a point ("PVT 43.430946 0.000000
20.000000 :: Generic error" in the 28 August logs).

Cost on the wire: 6 frames per point (write initiate, ack, 7-byte segment,
ack, 5-byte segment, ack), about 0.72 ms measured on the 1 Mbit/s bus, so
12 useful bytes occupy roughly 800 bits: a payload efficiency near 15%. The
kept lines hold about 8,600 points per drive; scaled by the monitor's keep
rate the master streams about 40 points a second per drive while an axis
moves, and PVT streaming is about a third of all frames on the bus; the
0x2014:01 status read is most of the rest.

If bus load ever matters: a point fits in two PDOs with no acknowledgements,
or in one 8-byte frame as 16-bit scaled position and velocity with the time
implied, which is how most CANopen motion profiles do it.

## The "10-second stalls": a capture artefact

An earlier version of this note read the 2 to 4 s gaps every 10 s in the
kept lines as the master stalling and the drives running out of trajectory
points mid-move. The monitor's per-second totals show the bus carrying
3,500 to 4,000 frames through every one of those gaps, and the 27F counter
advances normally across them, so the master did not stall and the drives
were fed. The buffer-underrun analysis built on those gaps is withdrawn.

Two things in that stretch are real: the master's heartbeat did stop for
5.9 s from 19:05:36 while it was reconfiguring the drives (all five drives
raised 0x8130, and the tool was keeping a third of frames through those
seconds), and after a drive runs out of points it holds position rather
than faulting, which the restart at 19:06:05 shows.

The genuine risk is unchanged in kind but not demonstrated here: a moving
drive holds 180 to 840 ms of points, so anything that stops the master
feeding it for longer than that stops the arm mid-move. A capture from a
monitor that keeps every frame is needed before saying whether that ever
happens in normal running.

## Bus utilisation

The bit rate is 1 Mbit/s: seconds with 4,286 frames of mostly 8-byte data,
about 56% of a 1 Mbit/s bus, could not exist at any slower rate. Figures use
the monitor's per-second frame totals (not the kept lines) and the mean
frame length from the kept mix, about 130 bits with worst-case stuffing.

| | 18:32 capture | 18:59 capture |
|---|---|---|
| Sustained, whole capture | error storm, not meaningful | 47% |
| Typical second | | 49% |
| Busiest second | | 56% |
| Drives stopped (19:05:47 to 19:06:10) | | under 1% |

So the bus runs at about half its capacity all the time the system is
working, at the top of the range normally accepted for a control bus. Status
reads of 0x2014:01 are about 61% of frames and PVT streaming about 32%;
either moving to PDOs would roughly halve the load.

