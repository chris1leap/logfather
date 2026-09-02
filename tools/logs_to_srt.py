import csv
from datetime import datetime, timedelta
from pathlib import Path

# ========= CONFIGURATION =========

INPUT_CSV = "Log file sample PP12.csv"   # path to your log file
OUTPUT_SRT = "log_subtitles.srt"        # output subtitle file

# Name of the timestamp column in the CSV:
TIME_COLUMN = "@timestamp_ros"

# Columns to include in the subtitle text (in order):
TEXT_COLUMNS = ["source", "state_name", "message"]

# How long each subtitle should stay on screen (seconds)
SUBTITLE_DURATION_SECONDS = 0.1

# --- Timestamp parsing ---
# Example format from your file: "16 Nov, 2025 @ 13:17:37.529"
TIMESTAMP_FORMAT = "%d %b, %Y @ %H:%M:%S.%f"

# ========= VIDEO SYNC OPTIONS =========
# Choose *one* of the two main ways to sync:

# 1) Align a specific real-world timestamp to video time 00:00:00
#    e.g. if your video recording started at "16 Nov, 2025 @ 13:17:30.000",
#    then the log entry that has that timestamp will appear at 00:00:00 in the video.
VIDEO_START_TIMESTAMP = "16 Nov, 2025 @ 13:01:33.000"
# Example:
# VIDEO_START_TIMESTAMP = "16 Nov, 2025 @ 13:17:30.000"

# 2) Additional manual offset in seconds
#    Positive values delay subtitles, negative values bring them earlier.
#    For example, if subtitles appear 2.3 seconds too early, set this to +2.3.
MANUAL_OFFSET_SECONDS = 0.0

# If VIDEO_START_TIMESTAMP is None:
#  - time 0 in the video is set to the first log entry
# If VIDEO_START_TIMESTAMP is not None:
#  - time 0 in the video is that given wall-clock timestamp

# ======================================


def parse_timestamp(ts_str: str) -> datetime:
    """Parse the timestamp string into a datetime object."""
    ts_str = ts_str.strip()
    return datetime.strptime(ts_str, TIMESTAMP_FORMAT)


def format_srt_time(td: timedelta) -> str:
    """
    Convert a timedelta to SRT time format:
    HH:MM:SS,mmm
    """
    total_ms = int(td.total_seconds() * 1000)
    if total_ms < 0:
        total_ms = 0  # clamp negative times to 0:00:00,000

    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem = rem % 60_000
    seconds = rem // 1000
    ms = rem % 1000

    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def build_subtitle_text(row: dict) -> str:
    """
    Build the subtitle text from selected columns.
    Joins non-empty fields with " | ".
    """
    parts = []
    for col in TEXT_COLUMNS:
        value = row.get(col, "")
        if value is None:
            continue
        value = str(value).strip()
        if value and value != "-":
            parts.append(value)
    return " | ".join(parts)


def main():
    input_path = Path(INPUT_CSV)
    output_path = Path(OUTPUT_SRT)

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    # Read all log entries
    entries = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row.get(TIME_COLUMN, "").strip()
            if not ts_str:
                continue

            try:
                ts = parse_timestamp(ts_str)
            except Exception as e:
                print(f"Skipping row with bad timestamp '{ts_str}': {e}")
                continue

            text = build_subtitle_text(row)
            if not text:
                continue

            entries.append((ts, text))

    if not entries:
        raise SystemExit("No valid log entries found with timestamps and text.")

    # Sort entries by timestamp (just in case)
    entries.sort(key=lambda x: x[0])

    # Determine reference time = video time 00:00:00
    if VIDEO_START_TIMESTAMP:
        video_start_dt = parse_timestamp(VIDEO_START_TIMESTAMP)
    else:
        # Use first log timestamp as video time 0
        video_start_dt = entries[0][0]

    manual_offset = timedelta(seconds=MANUAL_OFFSET_SECONDS)

    # Build SRT cues
    srt_lines = []
    for idx, (log_dt, text) in enumerate(entries, start=1):
        # Convert log time to video-relative time
        video_td = (log_dt - video_start_dt) + manual_offset

        start_td = video_td
        end_td = video_td + timedelta(seconds=SUBTITLE_DURATION_SECONDS)

        start_str = format_srt_time(start_td)
        end_str = format_srt_time(end_td)

        srt_lines.append(str(idx))
        srt_lines.append(f"{start_str} --> {end_str}")
        srt_lines.append(text)
        srt_lines.append("")  # blank line between cues

    # Write SRT file
    with output_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    print(f"Wrote {len(entries)} subtitles to {output_path}")


if __name__ == "__main__":
    main()
