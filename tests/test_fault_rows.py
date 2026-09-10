from logfather.ui.viewer_widgets import is_fault_row


def test_fault_rows_are_recognised():
    assert is_fault_row("07:25:02.863  |  /leap/manip1/act_controller | Fault on motor | servo 5: Current limit exceeded :: Current over limit")
    assert is_fault_row("07:25:02.863  |  /leap/manip1/act_controller | high_current_error | New node state")
    assert is_fault_row("07:39:45.813  |  /leap/manip1/sensors_digital_input_node | protective_stop_on | New node state")
    assert not is_fault_row("07:25:02.863  |  /leap/manip1/act_controller | Received service call")
    assert not is_fault_row("07:25:03.095  |  /leap/manip1/behaviour_node | caution | New system state")
