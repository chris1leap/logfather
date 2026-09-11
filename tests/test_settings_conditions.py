from logfather.data.settings_store import Settings


def test_motor_conditions_backfill_placeholder_slots_overcurrent_first():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    s = Settings._from_dict(data)
    assert s.conditions[12].name == "Motor overcurrent" and s.conditions[12].query == '"Current over limit"'
    assert s.conditions[13].name == "Motor other fault" and s.conditions[13].query == '"Fault on motor" AND NOT "Current over limit"'


def test_motor_fault_condition_leaves_a_used_slot_alone():
    data = {"conditions": [{"name": "Cond %d" % (i + 1), "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Mine", "query": '"something"', "color": "#123456"}
    s = Settings._from_dict(data)
    assert s.conditions[12].name == "Mine" and s.conditions[12].query == '"something"'


def test_saved_motor_pair_in_the_old_order_is_swapped_and_renamed():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Motor fault", "query": '"Fault on motor" AND NOT "Current over limit"', "color": "#ff7a45"}
    data["conditions"][13] = {"name": "Motor overcurrent", "query": '"Current over limit"', "color": "#ffd666"}
    s = Settings._from_dict(data)
    assert (s.conditions[12].name, s.conditions[12].query, s.conditions[12].color) == ("Motor overcurrent", '"Current over limit"', "#ffd666")
    assert (s.conditions[13].name, s.conditions[13].query) == ("Motor other fault", '"Fault on motor" AND NOT "Current over limit"')
    assert s.conditions[14].name == "Cond 15" and s.conditions[14].query == ""


def test_motor_fault_query_upgraded_from_the_old_preset_text():
    data = {"conditions": [{"name": f"Cond {i + 1}", "query": "", "color": ""} for i in range(15)]}
    data["conditions"][12] = {"name": "Motor fault", "query": '"Fault on motor"', "color": "#ff7a45"}
    s = Settings._from_dict(data)
    assert s.conditions[12].query == '"Fault on motor" AND NOT "Current over limit"'
    assert s.conditions[12].name == "Motor other fault"
