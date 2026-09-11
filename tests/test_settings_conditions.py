from logfather.data.settings_store import Settings


def test_motor_fault_condition_backfills_placeholder_slot():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    s = Settings._from_dict(data)
    fault = [c for c in s.conditions if c.name == "Motor fault"]
    assert len(fault) == 1 and fault[0].query == '"Fault on motor" AND NOT "Current over limit"' and fault[0].color == "#ff7a45"


def test_motor_fault_condition_leaves_a_used_slot_alone():
    data = {"conditions": [{"name": "Cond %d" % (i + 1), "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Mine", "query": '"something"', "color": "#123456"}
    s = Settings._from_dict(data)
    assert s.conditions[12].name == "Mine" and s.conditions[12].query == '"something"'


def test_motor_overcurrent_condition_backfills_slot_14():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    s = Settings._from_dict(data)
    assert s.conditions[13].name == "Motor overcurrent" and s.conditions[13].query == '"Current over limit"'
    assert s.conditions[14].name == "Cond 15" and s.conditions[14].query == ""


def test_motor_fault_query_upgraded_from_the_old_preset_text():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Motor fault", "query": '"Fault on motor"', "color": "#ff7a45"}
    s = Settings._from_dict(data)
    assert s.conditions[12].query == '"Fault on motor" AND NOT "Current over limit"'
