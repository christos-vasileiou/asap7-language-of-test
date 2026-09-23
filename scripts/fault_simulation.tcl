# Compatibility entry point for the authoritative vector adapter.
# Use tetramax_backend.py to generate REQUEST_FILE and supervise the licensed process.
if {![info exists env(REQUEST_FILE)]} {
    puts stderr "REQUEST_FILE is required; use tetramax_backend.py via fault_sim.py"
    exit 1
}
source [file join [file dirname [info script]] tmax_vector_fault_sim.tcl]
