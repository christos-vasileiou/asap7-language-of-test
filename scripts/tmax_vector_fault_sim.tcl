# =============================================================================
# TetraMAX: fault simulation for one externally supplied test pattern (STIL)
# =============================================================================
# Environment (see data_preprocessing/tmax.py build_env + extras):
#   CELL_LIBS_VERILOG   Structural ASAP7 cell .v libraries (read_netlist -library)
#   CELL_LIBS_LIBERTY   Liberty .lib paths (SEQ cell-name scan only)
#   LIBS                Legacy alias for CELL_LIBS_VERILOG
#   OUTPUT_DIR          Per-design output root
#   VERILOG_FILE        Gate-level design netlist
#   STIL_FILE           Single-pattern STIL (see tetramax_stil.write_vector_stil)
#   PATTERN_IDX         Pattern index for simulation/ artifact names (default 0)
# =============================================================================

proc split_space_separated_paths {raw} {
  set out {}
  foreach x [split $raw " "] {
    set t [string trim $x]
    if {$t ne ""} { lappend out $t }
  }
  return $out
}

if {[info exists env(CELL_LIBS_VERILOG)] && [string trim $env(CELL_LIBS_VERILOG)] ne ""} {
  set libs_list [split_space_separated_paths $env(CELL_LIBS_VERILOG)]
} elseif {[info exists env(LIBS)] && [string trim $env(LIBS)] ne ""} {
  set libs_list [split_space_separated_paths $env(LIBS)]
} else {
  puts "Error: Set CELL_LIBS_VERILOG or LIBS."
  exit 1
}

set seq_lib ""
if {[info exists env(SEQ_LIB)] && [string trim $env(SEQ_LIB)] ne ""} {
  set seq_lib [string trim $env(SEQ_LIB)]
} elseif {[info exists env(CELL_LIBS_LIBERTY)] && [string trim $env(CELL_LIBS_LIBERTY)] ne ""} {
  foreach lib [split_space_separated_paths $env(CELL_LIBS_LIBERTY)] {
    if {[string match {*SEQ*} $lib]} { set seq_lib $lib; break }
  }
}
if {$seq_lib eq ""} {
  foreach lib $libs_list {
    if {[string match {*SEQ*} $lib]} { set seq_lib $lib; break }
  }
}

proc assert_netlist_libs_only {paths} {
  foreach p $paths {
    if {$p eq ""} { continue }
    if {[string equal -nocase [file extension $p] ".lib"]} {
      puts "Error: CELL_LIBS_VERILOG must not include .lib: $p"
      exit 1
    }
  }
}

proc is_list {value} {
  return [expr {![catch {llength $value}]}]
}

set output_dir $env(OUTPUT_DIR)
set verilog_file $env(VERILOG_FILE)
set stil_file $env(STIL_FILE)

if {![file exists $verilog_file]} {
  puts "Error: Verilog file '$verilog_file' does not exist"
  exit 1
}
if {![file exists $stil_file]} {
  puts "Error: STIL file '$stil_file' does not exist"
  exit 1
}

set pattern_idx 0
if {[info exists env(PATTERN_IDX)]} {
  set pattern_idx [string trim $env(PATTERN_IDX)]
}

set design_name [file rootname [file tail $verilog_file]]
set design_output_dir [file join $output_dir $design_name]
file mkdir $design_output_dir
file mkdir [file join $design_output_dir simulation good]
file mkdir [file join $design_output_dir simulation bad]

# Combinational ASAP7 language-of-test netlists (sequential flows quit in full ATPG tmax.tcl)
set_netlist -nosequential_modeling

if {[is_list $libs_list]} {
  assert_netlist_libs_only $libs_list
  foreach lib $libs_list {
    catch {read_netlist $lib -library}
  }
} else {
  assert_netlist_libs_only [list $libs_list]
  catch {read_netlist $libs_list -library}
}

set read_result [catch {read_netlist $verilog_file} error_msg]
if {$read_result == 1} {
  puts "Error reading $verilog_file: $error_msg"
  exit 1
}

set_build -nonet_connections_change_netlist -nodelete_unused_gates
set drc_log [file join $design_output_dir "drc_vector_sim.txt"]
run_build_model > $drc_log
set_drc $stil_file
run_drc >> $drc_log

set_patterns -delete
set_patterns -external $stil_file -sensitive
remove_faults -all
add_faults -all

set goodMSFile [file join $design_output_dir simulation good machine_${pattern_idx}.txt]
set badMSFileFS [file join $design_output_dir simulation bad machine_faults_sim_${pattern_idx}.txt]
set badMSFileDF [file join $design_output_dir simulation bad machine_detected_faults_${pattern_idx}.csv]

run_simulation > $goodMSFile
run_fault_sim -ndetects 1 > $badMSFileFS
report_faults -all -collapsed > $badMSFileDF

set_drc -nofile
quit
