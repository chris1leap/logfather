"""Unit tests for the overview per-system summary state machine.

_summarize_system turns a day's raw Elastic events into SKU/manual
segments, stop markers, and a live status. It only touches
self._format_sku_parts (a staticmethod), so it is called unbound with
the class itself as ``self`` — no widget, no QApplication.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logfather.ui.overview_widget import OverviewSystemState, OverviewWidget

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
CUTOFF = T0 + timedelta(hours=8)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _state(events: list[dict]) -> OverviewSystemState:
    return OverviewSystemState(name="PikPak012", root=Path("Z:/public/PikPak012"), robot_id="35-2300-012", events=events)


def summarize(events: list[dict], cutoff: datetime = CUTOFF) -> dict:
    return OverviewWidget._summarize_system(OverviewWidget, _state(events), cutoff)


def _start(minutes: float, selection: dict | None = None) -> dict:
    return {"ts": _at(minutes), "state_name": "start_pnp", "selection": selection}


def _select(minutes: float, selection: dict) -> dict:
    return {"ts": _at(minutes), "state_name": "sku_selected", "selection": selection}


def _stop(minutes: float, state_name: str = "system_stop") -> dict:
    return {"ts": _at(minutes), "state_name": state_name}


def _manual(minutes: float) -> dict:
    return {"ts": _at(minutes), "state_name": "controller_node_manual_mode"}


def _auto(minutes: float) -> dict:
    return {"ts": _at(minutes), "state_name": "controller_node_automatic_mode"}


SKU_A = {"sku": "A123", "tray": "T1", "tool": "gripper"}
SKU_B = {"sku": "B456", "tray": "T2", "tool": "suction"}


class TestEmptyAndUnparsable:
    def test_no_events_is_unknown(self):
        summary = summarize([])
        assert summary["status"] == "Unknown"
        assert summary["sku_segments"] == []
        assert summary["manual_segments"] == []
        assert summary["stop_markers"] == []
        assert summary["faded_segment"] is None

    def test_unclassified_events_mean_idle(self):
        summary = summarize([{"ts": _at(0), "state_name": "heartbeat"}])
        assert summary["status"] == "Idle"

    def test_event_without_datetime_ts_is_skipped(self):
        summary = summarize([{"ts": "not-a-datetime", "state_name": "system_stop"}])
        assert summary["stop_markers"] == []


class TestSkuRuns:
    def test_start_opens_run_closed_at_cutoff(self):
        summary = summarize([_start(0, SKU_A)])
        assert summary["status"] == "Running"
        assert summary["current_sku"] == "A123 | T1 | gripper"
        assert summary["faded_segment"] == {"kind": "sku", "sku": "A123 | T1 | gripper"}
        [seg] = summary["sku_segments"]
        assert (seg["start"], seg["end"]) == (_at(0), CUTOFF)
        assert (seg["sku"], seg["tray"], seg["tool"]) == ("A123", "T1", "gripper")

    def test_stop_closes_run_and_marks(self):
        summary = summarize([_start(0, SKU_A), _stop(30)])
        assert summary["status"] == "Stopped"
        [seg] = summary["sku_segments"]
        assert (seg["start"], seg["end"]) == (_at(0), _at(30))
        [marker] = summary["stop_markers"]
        assert marker["ts"] == _at(30)
        assert marker["state_name"] == "system_stop"
        assert summary["faded_segment"] is None

    def test_sku_change_splits_segments(self):
        summary = summarize([_start(0, SKU_A), _select(30, SKU_B)])
        first, second = summary["sku_segments"]
        assert (first["start"], first["end"], first["sku"]) == (_at(0), _at(30), "A123")
        assert (second["start"], second["end"], second["sku"]) == (_at(30), CUTOFF, "B456")

    def test_reselecting_same_sku_does_not_split(self):
        summary = summarize([_start(0, SKU_A), _select(30, dict(SKU_A))])
        assert len(summary["sku_segments"]) == 1

    def test_start_without_selection_reuses_last_selection(self):
        summary = summarize([_select(0, SKU_A), _stop(10), _start(20)])
        [seg] = summary["sku_segments"]
        assert seg["sku"] == "A123"
        assert seg["start"] == _at(20)

    def test_events_are_sorted_by_time(self):
        summary = summarize([_stop(30), _start(0, SKU_A)])
        [seg] = summary["sku_segments"]
        assert (seg["start"], seg["end"]) == (_at(0), _at(30))


class TestManualMode:
    def test_manual_segment_closed_by_auto(self):
        summary = summarize([_manual(0), _auto(45)])
        [seg] = summary["manual_segments"]
        assert (seg["start"], seg["end"]) == (_at(0), _at(45))
        assert summary["status"] == "Auto"

    def test_open_manual_reports_manual_status(self):
        summary = summarize([_manual(0)])
        assert summary["status"] == "Manual"
        assert summary["faded_segment"] == {"kind": "manual", "sku": ""}
        [seg] = summary["manual_segments"]
        assert seg["end"] == CUTOFF

    def test_stop_during_manual_keeps_manual_status(self):
        summary = summarize([_manual(0), _stop(10)])
        assert summary["status"] == "Manual"
        assert len(summary["stop_markers"]) == 1

    def test_manual_interrupts_sku_run(self):
        summary = summarize([_start(0, SKU_A), _manual(30)])
        [sku_seg] = summary["sku_segments"]
        assert (sku_seg["start"], sku_seg["end"]) == (_at(0), _at(30))
        [manual_seg] = summary["manual_segments"]
        assert manual_seg["start"] == _at(30)
        assert summary["status"] == "Manual"


class TestStopDetection:
    def test_fuzzy_stop_state_names_count(self):
        for name in ("hardware_emergency_stop", "protective_stop", "caution_led_on", "EStop_active"):
            assert summarize([_stop(0, name)])["stop_markers"], name

    def test_shutdown_service_counts_as_stop(self):
        summary = summarize([{"ts": _at(0), "state_name": "", "service_name": "system_shutdown"}])
        assert len(summary["stop_markers"]) == 1


class TestFormatSkuParts:
    def test_joins_present_parts(self):
        assert OverviewWidget._format_sku_parts(SKU_A) == "A123 | T1 | gripper"

    def test_skips_empty_parts(self):
        assert OverviewWidget._format_sku_parts({"sku": "A123", "tray": "", "tool": None}) == "A123"

    def test_non_dict_is_empty(self):
        assert OverviewWidget._format_sku_parts(None) == ""
