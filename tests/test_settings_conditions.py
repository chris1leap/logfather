from logfather.data.settings_store import Settings


def test_motor_fault_condition_backfills_placeholder_slot():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    s = Settings._from_dict(data)
    fault = [c for c in s.conditions if c.name == "Motor fault"]
    assert len(fault) == 1 and fault[0].query == '"Fault on motor"' and fault[0].color == "#ff7a45"


def test_motor_fault_condition_leaves_a_used_slot_alone():
    data = {"conditions": [{"name": "Cond %d" % (i + 1), "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Mine", "query": '"something"', "color": "#123456"}
    s = Settings._from_dict(data)
    assert s.conditions[12].name == "Mine" and s.conditions[12].query == '"something"'
