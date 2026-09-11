from logfather.data.elastic_loader import condition_clause


def test_condition_clause_requires_own_state_for_state_changes():
    c = condition_clause('"crate_change_package_error"')
    assert c["bool"]["must"][0]["query_string"]["query"] == '"crate_change_package_error"'
    guard = c["bool"]["must_not"][0]["bool"]
    assert guard["filter"][0]["terms"]["message.keyword"] == ["New system state", "New node state"]
    assert guard["must_not"][0]["query_string"]["fields"] == ["state_name", "state_name.keyword"]
