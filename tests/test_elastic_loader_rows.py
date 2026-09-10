from logfather.data.elastic_loader import actuator_detail


def test_actuator_detail_fault_and_warning():
    assert actuator_detail({"fail_type": "Current limit exceeded :: Current over limit", "servo_id": "5"}) == "servo 5: Current limit exceeded :: Current over limit"
    assert actuator_detail({"update_info": "DS401: Input voltage too low :: Under Voltage", "servo_id": "2"}) == "servo 2: DS401: Input voltage too low :: Under Voltage"
    assert actuator_detail({"update_info": "Error Reset or No Error :: N/A"}) == "Error Reset or No Error :: N/A"
    assert actuator_detail({"message": "New node state", "servo_id": "3"}) == ""
    assert actuator_detail({}) == ""
