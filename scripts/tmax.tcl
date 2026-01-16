# OUTPUT_DIR=../output_random_pis
# VERILOG_DIR=../circuits_random_pis/verilog/*.v

# set the path to the directory containing verilog files
set verilog_dir [pwd] 

# Find the sequential library file in $env(LIBS) that contains "SEQ"
set libs_list [split $env(LIBS) " "]
set seq_lib ""
foreach lib $libs_list {
    if {[string match {*SEQ*} $lib]} {
        set seq_lib $lib
        break
    }
}

set fh [open $seq_lib r]
set seq_data [read $fh]
close $fh

# Extract all cell names (module and primitive names) from the loaded sequential library ($seq_data)

# Find all module names.
# Equivalent to: '|'.join(re.findall('module (.+?) \(', seq_data))
set moduleMatches [regexp -all -inline {module\s+([A-Za-z0-9_]+)\s*\(} $seq_data]
set moduleNames {}
foreach {fullMatch moduleName} $moduleMatches {
    lappend moduleNames $moduleName
}
set modulesString [join $moduleNames "|"]

# Find all primitive names.
# Equivalent to: '|'.join(re.findall('primitive (.+?) \(', seq_data))
set primitiveMatches [regexp -all -inline {primitive\s+([A-Za-z0-9_]+)\s*\(} $seq_data]
set primitiveNames {}
foreach {fullMatch primitiveName} $primitiveMatches {
    lappend primitiveNames $primitiveName
}
set primitivesString [join $primitiveNames "|"]

set seq_stdcell_names_regex "$modulesString|$primitivesString"
# $seq_stdcell_names_regex is now a safe TCL regex like Python's '|'.join(re.findall(...))


# Helper: detect whether a netlist appears to be sequential
proc is_sequential_netlist {filePath} {
  global seq_stdcell_names_regex
  if {![file exists $filePath]} {
    return 0
  }
  set fh [open $filePath r]
  set data [read $fh]
  close $fh

  # Common sequential cell names (gate-level)
  if {[regexp -nocase "\\y($seq_stdcell_names_regex)\\y" $data]} {
    return 1
  }

  return 0
}

set output_dir $env(OUTPUT_DIR)
set verilog_file $env(VERILOG_FILE)

if {![file exists $verilog_file]} {
  puts "Error: Verilog file '$verilog_file' does not exist"
  exit 1
}

# Extract the design name from the file name
set design_name [file rootname [file tail $verilog_file]]
echo $design_name

# Create a directory for this design's outputs
set design_output_dir [file join $output_dir $design_name]
file mkdir $design_output_dir
  
after 50

# Detect if combinational or sequential logic (from file contents)
set is_seq [is_sequential_netlist $verilog_file]
if {$is_seq} {
  set_netlist -sequential_modeling
  quit
} else {
  set_netlist -nosequential_modeling
}

proc is_list {value} {
    return [expr {![catch {llength $value}]}]
}

# 1. Read the default library
if {[is_list $libs_list]} {
  foreach lib $libs_list {
    set read_result [catch {read_netlist $lib -library >> .temp5.tcl} error_msg]
  }
} else {
  set read_result [catch {read_netlist $libs_list -library >> .temp5.tcl} error_msg]
} 

# 2. Read the Verilog netlist
set read_result [catch {read_netlist $verilog_file >> .temp5.tcl} error_msg]
rm .temp5.tcl

# Check if read_netlist was successful
if {$read_result == 1} {
  # Read netlist failed, print error message
  puts "Error reading $verilog_file: $error_msg"
}

# 3. Build simulation model
set_build -nonet_connections_change_netlist -nodelete_unused_gates 
set drc_output_file [file join $design_output_dir "drc.txt"]
run_build_model > $drc_output_file

# 4. Run Design Rule Check (DRC)
run_drc >> $drc_output_file
  
# 5. Set faults options
set_faults -fault_coverage -report uncollapsed
set_patterns -delete
set_patterns -internal
remove_faults -all > .temp.txt
add_faults -all >> .temp.txt
  
# Set -full_seq_atpg, if sequential logic is used
if {$is_seq} {
  set_atpg -full_seq_atpg -norandom_fill
}

# 6. Run ATPG and Report Coverage
set atpg_output_file [file join $design_output_dir "atpg.txt"]
run_atpg -ndetects 1 > $atpg_output_file

# 7. Report Patterns
set patterns_output_file [file join $design_output_dir "patterns.txt"]
report_patterns -all -internal > $patterns_output_file

# 8. Report Faults
set faults_output_file [file join $design_output_dir "faults.txt"]
report_faults -all > $faults_output_file

# 9. Create a file for fault simulation purposes. This file consists of the patterns and extra info ideal for pattern simulation. 
# format: .stil
set patterns_stil_output_file [file join $design_output_dir "simulation.stil"]
rm $patterns_stil_output_file
write_patterns $patterns_stil_output_file -replace -internal -format stil -nocompaction -nocycle_count -nopatinfo -serial -cellnames parallel

# 10. Create a testbench for simulation and state capture
set testbench_output_file [file join $design_output_dir "simulation.v"]
write_testbench -input $patterns_stil_output_file -output $testbench_output_file -replace

# Open the stil file and read its content
set file [open $patterns_stil_output_file]
set content [read $file]
close $file

# The following 4 commands are to parse the stil file and prepare
# Good machine and Bad machine simulation

set pattern_re {"pattern\s+([0-9]+)":\s*Call\s*"capture"\s*\{\s*"_pi"=([0-9]+);\s*"_po"=([A-Z]+);}

set header ""
set lines [split $content "\n"]
set found_pattern 0
foreach line $lines {
    if {!$found_pattern && [regexp {^\s*"pattern\s+[0-9]+":} $line]} {
        # Stop collecting header when we encounter the first pattern
        set found_pattern 1
        break
    }
    append header $line "\n"
}

set tail "\n}"

set resultList [regexp -all -inline $pattern_re $content]

set idx 0
set total [llength $resultList]
set count 0
while {$idx < $total} {
    # Skip the full match (index idx) – we don't need it here
    # The next items are the capturing groups
    set full     [lindex $resultList $idx]
    set patIndex [lindex $resultList [expr {$idx + 1}]]
    set pi       [lindex $resultList [expr {$idx + 2}]]
    set po       [lindex $resultList [expr {$idx + 3}]]
    set idx      [expr {$idx + 4}]

    set tempFilename [file join $design_output_dir "temp.stil"]
    # rm $tempFilename
    set tempfile [open $tempFilename "w"]
    # Write the preserved header to the output file
    puts -nonewline $tempfile $header

    # Write the new pattern call.  The index is always "pattern 0"
    puts $tempfile [format "   \"pattern 0\": Call \"capture\" {"]
    puts $tempfile [format "      \"_pi\"=%s;" $pi]
    puts $tempfile [format "      \"_po\"=%s; }" $po]

    # Append a closing brace for the Pattern block
    puts -nonewline $tempfile $tail
    close $tempfile

    set dumpNdeleteFile [file join $design_output_dir "temp.txt"]

    # Prepare tetraMax for simulation
    drc -force >> .temp.txt
    read_netlist -delete >> .temp.txt
    if {$is_seq} {
      set_netlist -sequential_modeling
    } else {
      set_netlist -nosequential_modeling
    }
    
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
    set_drc $tempFilename
    run_drc >> $dumpNdeleteFile
    set_patterns -delete
    set_patterns -external $tempFilename -sensitive
    remove_faults -all >> .temp.txt
    add_faults -all >> .temp.txt

    # Clean up un-wanted files
    rm $tempFilename

    #folders to store the simulation files in
    set badMSdir [file join $design_output_dir "simulation/bad/"]
    file mkdir $badMSdir
    after 50
      
    set goodMSdir [file join $design_output_dir "simulation/good/"]
    file mkdir $goodMSdir
    after 50

    # good machine simulation
    set goodMSFile [file join $goodMSdir "machine_${count}.txt"]
    run_simulation > $goodMSFile
    # bad machine simulation
    set badMSFileFS [file join $badMSdir "machine_faults_sim_${count}.txt"]
    set badMSFileDF [file join $badMSdir "machine_detected_faults_${count}.csv"]
    run_fault_sim -ndetects 1 > $badMSFileFS
    report_faults -all -collapsed > $badMSFileDF

    # Increase counter of reports
    incr count

    # Delete temp info
    rm $dumpNdeleteFile
    set_drc -nofile
}


if {$is_seq} {
  # Restore defaults after per-pattern simulation steps
  set_atpg -nofull_seq_atpg -random_fill
}

# whether the circuit is sequential or not
set is_sequential_output_file [file join $design_output_dir "is_sequential.txt"]
set fh [open $is_sequential_output_file "w"]
puts $fh $is_sequential
close $fh

# Clean up. Go to the initial state.
set_drc -nofile
drc -force >> .temp.txt
read_netlist -delete >> .temp.txt
rm .temp.txt

puts "\n\nATPG process completed.\n\n"
quit
