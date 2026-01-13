# Formality verification script
# Compares synthesized netlist (implementation) with RTL (reference)
# Environment variables:
#   RTL_PATH: Directory containing the original RTL design (reference/golden)
#             The RTL file is assumed to be at $env(RTL_PATH)/design.v
#   SYNTHESIZED_FILE: Synthesized netlist (implementation to verify)
#   DESIGN: Top-level module name
#   DBS: Space-separated list of .db library files

remove_container r
remove_container i

# Read original RTL as reference (r = reference, the golden design)
# Construct RTL file path from RTL_PATH directory
set rtl_file [file join $env(RTL_PATH) design.v]
read_verilog -container r -libname WORK -05 $rtl_file
set_top r:/WORK/$env(DESIGN)
read_db [ split $env(DBS) ]

# Read synthesized netlist as implementation (i = implementation, to verify against reference)
read_verilog -container i -libname WORK -05 $env(SYNTHESIZED_FILE)
set_top i:/WORK/$env(DESIGN)

# Set verification limits
set verification_failing_point_limit 1

# Run verification
match
verify
cputime
exit
