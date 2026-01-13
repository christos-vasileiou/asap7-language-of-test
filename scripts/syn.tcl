# Design Compiler synthesis script
# Reads RTL from $env(RTL_PATH) and compiles top $env(DESIGN)
# Requires $env(DB) pointing to the library .db file

# ------------------------------------------------------------------
# Build target/link libraries – support *multiple* .db files passed in
# DBS (space-separated). Fallback to single DB
# for backward compatibility.
# ------------------------------------------------------------------
set target_library [split $env(DBS)]

# Always prepend the generic "*" library so DC can find wire-load etc.
set link_library [concat "*" $target_library]

# Ensure symbol library matches target library (for GUI)
set symbol_library $target_library

set language $env(SYN_LANGUAGE)
set rtl_path $env(RTL_PATH)

file mkdir $env(REPORTS_DIR)
file mkdir $env(RESULTS_DIR)

# define working directory of the given design
define_design_lib WORK -path $rtl_path/WORK

set pattern "*.v"

set verilog_files {}
foreach src [glob -nocomplain -directory $rtl_path $pattern] {
    lappend verilog_files $src
}

# Determine top-level design automatically if not provided via DESIGN
if { [info exists env(DESIGN)] && $env(DESIGN) ne "" } {
    set top_design $env(DESIGN)
} else {
    puts "Error: Please set properly DESIGN environment variable "
    exit 1
}

read_verilog $verilog_files -rtl

# link the design to the specified libraries
link

# check for unmapped logic
#if {[check_design -unmapped -no_warnings] != 1} {
#    puts "Error: design contains unmapped logic"
#    exit 2
#}

# Make unique copies of re-used submodules so they can be flattened independently
uniquify

# FLATTEN EVERYTHING under the current_design
ungroup -all -flatten

# ------------------------------------------------------------------
# Clock-gating setup: Robust clock detection and creation
# ------------------------------------------------------------------
# Search all ports for names containing "clk" or "clock" (case-insensitive)
# This handles various clock naming patterns: clock, clk, CLK, sys_clk, etc.

set clock_ports [list]
set all_ports [get_ports -quiet -hier]

if { [sizeof_collection $all_ports] > 0 } {
    foreach_in_collection port $all_ports {
        set port_name [get_object_name $port]
        set port_name_lower [string tolower $port_name]
        # Check if port name contains "clk" or "clock" (case-insensitive)
        if { [string match "*clk*" $port_name_lower] || [string match "*clock*" $port_name_lower] } {
            if { [lsearch -exact $clock_ports $port_name] == -1 } {
                lappend clock_ports $port_name
            }
        }
    }
}

# Create clocks for all found clock ports
if { [llength $clock_ports] > 0 } {
    set CLK_PERIOD 1000
    foreach clk_port $clock_ports {
        puts "Info: Creating clock on port: $clk_port"
        create_clock [get_ports $clk_port] -period $CLK_PERIOD -name [join [list "clk" $clk_port] "_"]
    }
} else {
    puts "Warning: No clock ports detected. Skipping create_clock."
    puts "Info: This may be intentional for combinational-only designs."
}

compile_ultra

write -format verilog -hierarchy -output $env(RESULTS_DIR)/${top_design}.v
# write -format ddc     -hierarchy -output $env(RESULTS_DIR)/${top_design}.ddc
# write -format ndm     -hierarchy -output $env(RESULTS_DIR)/${top_design}.ndm

# report_timing -path full -delay max -max_paths 10 > $env(REPORTS_DIR)/${top_design}.rpt
# report_area > $env(REPORTS_DIR)/${top_design}_area.rpt
# report_reference > $env(REPORTS_DIR)/${top_design}_references.rpt



# ------------------------------------------------------------------
# Collect design information and write to JSON
# ------------------------------------------------------------------
# Everything DC considers sequential (FFs + latches)
set n_seq [sizeof_collection [get_cells -hier -filter {is_sequential==true}]]

# Store whether design contains sequential logic
set has_sequential [expr {$n_seq > 0}]

set total_cells [sizeof_collection [get_cells -hier]]
set total_ports [sizeof_collection [get_ports -hier]]
set sequential_cells [sizeof_collection [get_cells -hier -filter {is_sequential==true}]]
set combinational_cells [expr {$total_cells - $sequential_cells}]

# Open JSON file for writing
set json_file [open "$env(RESULTS_DIR)/${top_design}_info.json" w]

puts $json_file "\{"
puts $json_file "  \"design_name\": \"$top_design\","
if { $has_sequential } {
    puts $json_file "  \"has_sequential_logic\": true,"
} else {
    puts $json_file "  \"has_sequential_logic\": false,"
}
puts $json_file "  \"sequential_cell_count\": $sequential_cells,"
puts $json_file "  \"combinational_cell_count\": $combinational_cells,"
puts $json_file "  \"total_cell_count\": $total_cells,"
puts $json_file "  \"total_port_count\": $total_ports,"
puts $json_file "  \"clock_ports\": \["
if { [llength $clock_ports] > 0 } {
    set first 1
    foreach clk_port $clock_ports {
        if { !$first } {
            puts $json_file ","
        }
        puts -nonewline $json_file "    \"$clk_port\""
        set first 0
    }
    puts $json_file ""
}
puts $json_file "  \],"
puts $json_file "  \"clock_port_count\": [llength $clock_ports]"
puts $json_file "\}"

close $json_file

puts "Info: Design information written to $env(RESULTS_DIR)/${top_design}_info.json"

quit

