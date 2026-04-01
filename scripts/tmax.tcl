# =============================================================================
# TetraMAX TCL driver: ATPG, STIL export, and per-pattern fault simulation
# =============================================================================
# Expected environment variables:
#   CELL_LIBS_VERILOG   Preferred. Space-separated **stdcell / PDK library** Verilog
#                       (.v) paths for read_netlist -library. These are NOT design
#                       netlists; they are structural cell models (e.g. asap7 *_AO_*.v).
#   CELL_LIBS_LIBERTY   Space-separated Liberty .lib paths (timing/CCS). Never passed
#                       to read_netlist; used to locate sequential cell names (*SEQ*)
#                       and for your own downstream STA/synthesis flows.
#   LIBS                Legacy alias: same role as CELL_LIBS_VERILOG if the latter
#                       is unset. Do not put .lib files here.
#   OUTPUT_DIR          Root directory for per-design outputs
#   VERILOG_FILE        Gate-level **design** netlist under test (separate from libs)
#   SEQ_LIB             Optional override: one file to scan for sequential cell names
#                       (.lib or .v). Otherwise first *SEQ* in CELL_LIBS_LIBERTY, else
#                       first *SEQ* in CELL_LIBS_VERILOG/LIBS.
#
# The flow: load libs + design, build model, run DRC/ATPG, write STIL, then parse
# STIL into individual patterns and run good-machine and bad-machine sims per pattern.
# =============================================================================

# set the path to the directory containing verilog files
set verilog_dir [pwd]

proc split_space_separated_paths {raw} {
  set out {}
  foreach x [split $raw " "] {
    set t [string trim $x]
    if {$t ne ""} {
      lappend out $t
    }
  }
  return $out
}

# -----------------------------------------------------------------------------
# Block: Cell-library Verilog list (structural .v only) for read_netlist -library
# -----------------------------------------------------------------------------
if {[info exists env(CELL_LIBS_VERILOG)] && [string trim $env(CELL_LIBS_VERILOG)] ne ""} {
  set libs_list [split_space_separated_paths $env(CELL_LIBS_VERILOG)]
} elseif {[info exists env(LIBS)] && [string trim $env(LIBS)] ne ""} {
  set libs_list [split_space_separated_paths $env(LIBS)]
} else {
  puts "Error: Set CELL_LIBS_VERILOG (preferred) or LIBS to space-separated structural cell-library Verilog (.v) paths."
  exit 1
}

# -----------------------------------------------------------------------------
# Block: Reference file for sequential cell *names* (text scan only; not read_netlist)
# -----------------------------------------------------------------------------
# Order: SEQ_LIB; else first *SEQ* in CELL_LIBS_LIBERTY; else first *SEQ* in libs_list.
# Source may be Liberty (cell (...)) or Verilog (module ... ()).
# -----------------------------------------------------------------------------
set seq_lib ""
if {[info exists env(SEQ_LIB)] && [string trim $env(SEQ_LIB)] ne ""} {
  set seq_lib [string trim $env(SEQ_LIB)]
} elseif {[info exists env(CELL_LIBS_LIBERTY)] && [string trim $env(CELL_LIBS_LIBERTY)] ne ""} {
  foreach lib [split_space_separated_paths $env(CELL_LIBS_LIBERTY)] {
    if {[string match {*SEQ*} $lib]} {
      set seq_lib $lib
      break
    }
  }
}
if {$seq_lib eq ""} {
  foreach lib $libs_list {
    if {[string match {*SEQ*} $lib]} {
      set seq_lib $lib
      break
    }
  }
}

if {$seq_lib eq "" || ![file exists $seq_lib]} {
  puts "Error: No sequential library reference for cell-name scan."
  puts "  Set SEQ_LIB, or include a path matching *SEQ* in CELL_LIBS_LIBERTY or CELL_LIBS_VERILOG."
  exit 1
}

set fh [open $seq_lib r]
set seq_data [read $fh]
close $fh

# -----------------------------------------------------------------------------
# Block: Parse sequential library text for cell / module names
# -----------------------------------------------------------------------------
set cell_names {}

if {[string equal -nocase [file extension $seq_lib] ".lib"]} {
  set pattern {cell\s*\(\s*([^)]+?)\s*\)\s*\{}
} else {
  set pattern {module\s+([^(\s]+)\s*\(}
}

set start 0
while {[regexp -indices -start $start $pattern $seq_data match range]} {
    set captures [regexp -inline -start $start $pattern $seq_data]
    set cell_name [string trim [lindex $captures 1]]
    lappend cell_names $cell_name
    set matched_range [lindex $range 1]
    set start [expr {$matched_range + 1}]
}

# -----------------------------------------------------------------------------
# Block: Build one big alternation regex: cell1|cell2|...
# -----------------------------------------------------------------------------
# Cell names may contain regex metacharacters; escape them so the pattern is literal.
# Word boundaries (\y) will be applied when scanning the user netlist (see proc below).
# -----------------------------------------------------------------------------
set escaped_names {}
foreach name $cell_names {
    set escaped $name
    regsub -all {[][(){}.^$*+?|\\]} $escaped {\\&} escaped
    lappend escaped_names $escaped
}
set seq_stdcell_names_regex [join $escaped_names "|"]


# -----------------------------------------------------------------------------
# Proc: is_sequential_netlist
# -----------------------------------------------------------------------------
# Returns 1 if the Verilog file text contains an instantiation or reference that
# matches any sequential cell name from the SEQ library (whole "word" via \y).
# Used to choose -sequential_modeling vs -nosequential_modeling for the netlist reader.
# -----------------------------------------------------------------------------
proc is_sequential_netlist {filePath} {
  global seq_stdcell_names_regex
  if {$seq_stdcell_names_regex eq ""} {
    return 0
  }
  if {![file exists $filePath]} {
    return 0
  }
  set fh [open $filePath r]
  set data [read $fh]
  close $fh

  # Match any known sequential stdcell name as a whole token in the netlist
  if {[regexp -nocase "\\y($seq_stdcell_names_regex)\\y" $data]} {
    return 1
  }

  return 0
}

# -----------------------------------------------------------------------------
# Block: Resolve paths and validate the input Verilog
# -----------------------------------------------------------------------------
set output_dir $env(OUTPUT_DIR)
set verilog_file $env(VERILOG_FILE)

if {![file exists $verilog_file]} {
  puts "Error: Verilog file '$verilog_file' does not exist"
  exit 1
}

# Design name = basename of netlist without extension (used for output subdirectory)
set design_name [file rootname [file tail $verilog_file]]
echo $design_name

# Per-design folder: $OUTPUT_DIR/<design_name>/...
set design_output_dir [file join $output_dir $design_name]
file mkdir $design_output_dir

# Small delay (workaround for filesystem or tool startup ordering in some environments)
after 50

# -----------------------------------------------------------------------------
# Block: Configure netlist mode from combinational vs sequential detection
# -----------------------------------------------------------------------------
# If sequential elements are found, enable sequential modeling for read_netlist.
# NOTE: The next two lines call quit — so for sequential designs the script exits here
# and the ATPG / STIL / per-pattern simulation below never runs. Only combinational
# designs continue past this point (unless this quit is removed for your use case).
# -----------------------------------------------------------------------------
set is_seq [is_sequential_netlist $verilog_file]
if {$is_seq} {
  set_netlist -sequential_modeling
  quit
} else {
  set_netlist -nosequential_modeling
}

# -----------------------------------------------------------------------------
# Proc: is_list — true if value looks like a Tcl list (llength succeeds)
# -----------------------------------------------------------------------------
# libs_list comes from CELL_LIBS_VERILOG or LIBS (space-separated); may be one path.
# -----------------------------------------------------------------------------
proc is_list {value} {
    return [expr {![catch {llength $value}]}]
}

# Liberty/timing sources cannot be loaded with read_netlist — fail fast with a clear message.
proc assert_netlist_libs_only {paths} {
  foreach p $paths {
    if {$p eq ""} { continue }
    set ext [file extension $p]
    if {[string equal -nocase $ext ".lib"]} {
      puts "Error: CELL_LIBS_VERILOG / LIBS must not include Liberty .lib files: $p"
      puts "  read_netlist only accepts structural netlists (e.g. PDK cell .v). Put .lib in CELL_LIBS_LIBERTY."
      exit 1
    }
  }
}

# -----------------------------------------------------------------------------
# Block: 1 — Read technology / cell libraries into TetraMAX
# -----------------------------------------------------------------------------
# read_netlist ... -library loads lib cells without treating them as top-level design.
# Only structural formats (Verilog, VHDL, EDIF, ...). Never .lib.
# Errors are caught; some flows append messages to .temp5.tcl then remove it.
# -----------------------------------------------------------------------------
if {[is_list $libs_list]} {
  assert_netlist_libs_only $libs_list
  foreach lib $libs_list {
    set read_result [catch {read_netlist $lib -library >> .temp5.tcl} error_msg]
  }
} else {
  assert_netlist_libs_only [list $libs_list]
  set read_result [catch {read_netlist $libs_list -library >> .temp5.tcl} error_msg]
} 

# -----------------------------------------------------------------------------
# Block: 2 — Read the design Verilog netlist (the circuit under test)
# -----------------------------------------------------------------------------
set read_result [catch {read_netlist $verilog_file >> .temp5.tcl} error_msg]
rm .temp5.tcl

if {$read_result == 1} {
  puts "Error reading $verilog_file: $error_msg"
}

# -----------------------------------------------------------------------------
# Block: 3 — Elaborate flat simulation model
# -----------------------------------------------------------------------------
# -nonet_connections_change_netlist: do not alter netlist when building connections
# -nodelete_unused_gates: keep gates that might look unused (often needed for ATPG)
# DRC text from build is redirected to drc.txt in the design output directory.
# -----------------------------------------------------------------------------
set_build -nonet_connections_change_netlist -nodelete_unused_gates 
set drc_output_file [file join $design_output_dir "drc.txt"]
run_build_model > $drc_output_file

# -----------------------------------------------------------------------------
# Block: 4 — Design rule check on the built model
# -----------------------------------------------------------------------------
run_drc >> $drc_output_file

# -----------------------------------------------------------------------------
# Block: 5 — Fault list and pattern container setup for ATPG
# -----------------------------------------------------------------------------
# -fault_coverage -report uncollapsed: track/report uncollapsed fault views
# -internal: use internal pattern representation
# remove/add_faults -all: start from a clean full fault list
# -----------------------------------------------------------------------------
set_faults -fault_coverage -report uncollapsed
set_patterns -delete
set_patterns -internal
remove_faults -all > .temp.txt
add_faults -all >> .temp.txt
  
# For sequential ATPG (not reached if script quit earlier for is_seq): constrain ATPG
if {$is_seq} {
  set_atpg -full_seq_atpg -norandom_fill
}

# -----------------------------------------------------------------------------
# Block: 6 — Run ATPG (single detection per fault for this script)
# -----------------------------------------------------------------------------
set atpg_output_file [file join $design_output_dir "atpg.txt"]
run_atpg -ndetects 1 > $atpg_output_file
  
# -----------------------------------------------------------------------------
# Block: 7 — Dump generated test patterns (internal form) to patterns.txt
# -----------------------------------------------------------------------------
set patterns_output_file [file join $design_output_dir "patterns.txt"]
report_patterns -all -internal > $patterns_output_file
  
# -----------------------------------------------------------------------------
# Block: 8 — Fault coverage / fault list report
# -----------------------------------------------------------------------------
set faults_output_file [file join $design_output_dir "faults.txt"]
report_faults -all > $faults_output_file

# -----------------------------------------------------------------------------
# Block: 9 — Write STIL for downstream simulators / tools
# -----------------------------------------------------------------------------
# STIL contains patterns plus timing/format info. Options reduce compaction and extras
# so downstream parsing stays simple. -serial -cellnames parallel control how cells
# are named in the serial pattern data.
# -----------------------------------------------------------------------------
set patterns_stil_output_file [file join $design_output_dir "simulation.stil"]
rm $patterns_stil_output_file
write_patterns $patterns_stil_output_file -replace -internal -format stil -nocompaction -nocycle_count -nopatinfo -serial -cellnames parallel

# -----------------------------------------------------------------------------
# Block: 10 — Verilog testbench that applies the STIL patterns
# -----------------------------------------------------------------------------
set testbench_output_file [file join $design_output_dir "simulation.v"]
write_testbench -input $patterns_stil_output_file -output $testbench_output_file -replace

# -----------------------------------------------------------------------------
# Block: Parse STIL — split one multi-pattern file into per-pattern chunks
# -----------------------------------------------------------------------------
# Goal: for each ATPG pattern, rerun a minimal TetraMAX session, load only that
# pattern from a tiny STIL, and run:
#   - run_simulation        → good machine (fault-free) responses
#   - run_fault_sim         → bad machine (with faults), plus detected-fault report
# The STIL is split on the Pattern "_pattern_" { ... } region.
# (?s) = dot matches newline, for multiline pattern section.
# -----------------------------------------------------------------------------
set file [open $patterns_stil_output_file]
set content [read $file]
close $file

# Capture everything before the pattern block, and the body inside Pattern "_pattern_" { ... }
regexp {(?s)(.*)Pattern "_pattern_" \{(.*)\}} $content -> beforePattern patternSection

# Header line that must be rewritten before each mini-STIL file
set commonPatternHeader "Pattern \"_pattern_\" \{"

# Split the pattern section into lines; pattern payloads are consumed in the loop below
set lines [split $patternSection '\n']
  
# First two lines after the opening brace are kept as fixed prefix for each mini-STIL
set firstLine [lindex $lines 1] 
set secondLine [lindex $lines 2]

# -----------------------------------------------------------------------------
# Block: Per-pattern loop — build temp STIL, reload design, simulate good/bad
# -----------------------------------------------------------------------------
# Lines 3..end are walked two lines at a time (even index ends a pattern): the script
# accumulates lines into $pattern, then on even $i wraps a mini-STIL file.
# Pattern indices inside the chunk are normalized to "pattern 0" for the tool.
# For each pattern: drc -force + read_netlist -delete clears state; libraries and
# design are re-read; build + DRC; external pattern file loads that single pattern;
# then good and bad simulations write machine_*.txt and CSV under simulation/good|bad.
# -----------------------------------------------------------------------------
set patterns {}
set pattern ""
set count 0
for {set i 3} {$i < [llength $lines]} {incr i} {
  append pattern [lindex $lines $i] "\n"
  if {$i % 2 ==0} {
    # Normalize pattern index inside this chunk to 0 (tool expects single pattern).
    # Note: original flow stores regsub result in modifiedString but writes $pattern below;
    # use $modifiedString in puts if the tool requires the rewritten index.
    set modifiedString [regsub {pattern (\d+)} $pattern "pattern 0"]

    # Write one minimal STIL: preamble + header + fixed lines + this pattern + closing brace
    set tempFilename [file join $design_output_dir "temp.stil"]
    # rm $tempFilename
    set tempfile [open $tempFilename "w"]
    puts $tempfile $beforePattern
    puts $tempfile $commonPatternHeader
    puts $tempfile $firstLine
    puts $tempfile $secondLine
    puts $tempfile $pattern
    puts $tempfile "\}\n"
    close $tempfile

    set dumpNdeleteFile [file join $design_output_dir "temp.txt"]

    # Reset TetraMAX netlist/pattern state and reload libs + design (combinational mode)
    drc -force >> .temp.txt
    read_netlist -delete >> .temp.txt
    set_netlist -nosequential_modeling
    
    if {[is_list $libs_list]} {
      foreach lib $libs_list {
        set read_result [catch {read_netlist $lib -library >> .temp5.tcl} error_msg]
      }
    } else {
      set read_result [catch {read_netlist $libs_list -library >> .temp5.tcl} error_msg]
    }

    read_netlist $verilog_file
    set_build -nonet_connections_change_netlist -nodelete_unused_gates 
    run_build_model > $dumpNdeleteFile
    # Point DRC at the single-pattern STIL; run_drc accepts it as pattern protocol
    set_drc $tempFilename
    run_drc >> $dumpNdeleteFile
    set_patterns -delete
    set_patterns -external $tempFilename -sensitive
    remove_faults -all >> .temp.txt
    add_faults -all >> .temp.txt

    rm $tempFilename

    # Output directories for this pattern index
    set badMSdir [file join $design_output_dir "simulation/bad/"]
    file mkdir $badMSdir
    after 50
      
    set goodMSdir [file join $design_output_dir "simulation/good/"]
    file mkdir $goodMSdir
    after 50

    # Good machine: no fault injection — expected "golden" behavior
    set goodMSFile [file join $goodMSdir "machine_${count}.txt"]
    run_simulation > $goodMSFile
    # Bad machine: fault simulation + which faults are detected for this pattern
    set badMSFileFS [file join $badMSdir "machine_faults_sim_${count}.txt"]
    set badMSFileDF [file join $badMSdir "machine_detected_faults_${count}.csv"]
    run_fault_sim -ndetects 1 > $badMSFileFS
    report_faults -all -collapsed > $badMSFileDF
      
    incr count
      
    lappend patterns $pattern
    set pattern ""

    rm $dumpNdeleteFile
    set_drc -nofile
  }
}

# Restore ATPG defaults if sequential mode had been enabled (unreachable if quit above)
if {$is_seq} {
  set_atpg -nofull_seq_atpg -random_fill
}

# -----------------------------------------------------------------------------
# Block: Final cleanup — release DRC file, clear netlists, exit
# -----------------------------------------------------------------------------
set_drc -nofile
drc -force >> .temp.txt
read_netlist -delete >> .temp.txt
#rm .temp.txt

#rm -rf '/proj/txace/cxv200006/transformers_atpg/.split_patterns/'

puts "\n\nATPG process completed.\n\n"
quit
