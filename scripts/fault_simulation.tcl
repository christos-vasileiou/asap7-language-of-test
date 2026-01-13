read_netlist $env(SYNTHESIZED_FILE)
set libs_list [split $env(LIBS) " "]

# 1. Read the default library
if {[is_list $libs_list]} {
  foreach lib $libs_list {
    set read_result [catch {read_netlist $lib -library >> .temp5.tcl} error_msg]
  }
} else {
  set read_result [catch {read_netlist $libs_list -library >> .temp5.tcl} error_msg]
} 

run_build_model
run_drc


