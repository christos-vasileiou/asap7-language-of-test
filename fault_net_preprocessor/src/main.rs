use clap::Parser;
use glob::glob;
use indicatif::{ProgressBar, ProgressStyle};
use rayon::prelude::*;
use regex::{Regex, RegexBuilder};
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Random Circuit Generator
#[derive(Parser)]
struct Args {
    /// Circuit folder
    #[arg(short = 'c', long = "circuit_folder")]
    circuit_folder: String,

    /// Output folder
    #[arg(short = 'o', long = "output_folder")]
    output_folder: String,

    /// Starting point
    #[arg(short = 's', long = "starting_point", default_value_t = 0)]
    starting_point: usize,

    /// Total number of shards for distributed execution
    #[arg(long = "shard_count", default_value_t = 1)]
    shard_count: usize,

    /// Index of the shard to process (0-based)
    #[arg(long = "shard_index", default_value_t = 0)]
    shard_index: usize,
}

fn main() {
    let args = Args::parse();

    let circuits_folder = args.circuit_folder;
    let tetramax_folder = args.output_folder;
    let start_idx = args.starting_point;
    let shard_count = args.shard_count.max(1);
    let shard_index = args.shard_index;
    if shard_index >= shard_count {
        eprintln!(
            "Shard index {} is out of range for shard count {}. Nothing to process.",
            shard_index, shard_count
        );
        return;
    }

    // Collect Verilog files (non-recursive) from the circuits folder. In the Python script,
    // only files with a `.v` extension in the top-level directory are considered.
    let pattern = format!("{}/*.v", circuits_folder);
    let mut verilog_files: Vec<PathBuf> = glob(&pattern)
        .expect("Failed to read glob pattern")
        .filter_map(Result::ok)
        .collect();
    verilog_files.sort();

    // Apply the starting index if provided
    let verilog_files: Vec<PathBuf> = if start_idx < verilog_files.len() {
        verilog_files[start_idx..].to_vec()
    } else {
        Vec::new()
    };

    // Filter files by shard assignment
    let verilog_files: Vec<PathBuf> = verilog_files
        .into_iter()
        .enumerate()
        .filter_map(|(idx, path)| {
            if idx % shard_count == shard_index {
                Some(path)
            } else {
                None
            }
        })
        .collect();

    // Compile the regular expression used to parse each instance line in the Verilog netlist.
    // This pattern mirrors the Python `NETS_RE` with verbose and multi-line flags.
    let nets_re = RegexBuilder::new(
        r"^ \s*
        (?P<cell>\w+)              # cell type
        \s+
        (?P<inst>\w+)              # instance name
        \s* \(
        \s*
        (?P<pins>
            \.\w+\s*\(\s*[^()]*\s*\)            # .PIN(expr)
            (?: \s*,\s* \.\w+\s*\(\s*[^()]*\s*\) )*   # , .PIN(expr) ...
        )
        \s* \)
        \s* ;
        \s*$"
    )
    .multi_line(true)
    .ignore_whitespace(true)
    .build()
    .expect("Failed to compile nets regular expression");

    // Compile a secondary regex to extract pin names and net names from the pins string.
    let pins_re = Regex::new(r"\.([A-Za-z0-9_]+)\s*\(\s*([^()]*)\s*\)")
        .expect("Failed to compile pins regular expression");

    // Wrap shared data in `Arc` for thread-safe sharing among Rayon workers.
    let tetramax_folder = Arc::new(tetramax_folder);
    let nets_re = Arc::new(nets_re);
    let pins_re = Arc::new(pins_re);

    // Set up a progress bar to mirror the tqdm progress indicator in the Python script.
    let pb = ProgressBar::new(verilog_files.len() as u64);
    pb.set_style(
        ProgressStyle::with_template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({eta}) {msg}")
            .unwrap()
            .progress_chars("#>-"),
    );
    let pb = Arc::new(pb);

    // Process each Verilog file in parallel. After each file is processed, increment the progress bar.
    verilog_files.par_iter().for_each(|verilog_file| {
        process_faults(
            verilog_file.clone(),
            tetramax_folder.clone(),
            nets_re.clone(),
            pins_re.clone(),
        );
        pb.inc(1);
    });
    pb.finish_with_message("Processing completed.");
}

/// Process faults for a given Verilog file
fn process_faults(
    verilog_file: PathBuf,
    tetramax_folder: Arc<String>,
    nets_re: Arc<Regex>,
    pins_re: Arc<Regex>,
) {
    let mut nets_mapping: HashMap<String, String> = HashMap::new();

    // Read the Verilog netlist. If it fails, log the error and return early.
    let netlist = match fs::read_to_string(&verilog_file) {
        Ok(content) => content,
        Err(e) => {
            eprintln!("Failed to read file {:?}: {}", verilog_file, e);
            return;
        }
    };

    // Find all instance declarations matching the regex. For each match, extract the instance
    // name and its pin assignments, then build a mapping from `inst/pin` to the connected net.
    for caps in nets_re.captures_iter(&netlist) {
        // Extract the instance name and the substring containing all pins
        let inst = caps.name("inst").unwrap().as_str().trim();
        let pins = caps.name("pins").unwrap().as_str();

        // For each `.PIN(expr)` pair, capture the pin name and the net expression
        for pin_cap in pins_re.captures_iter(pins) {
            let pin_name = pin_cap.get(1).unwrap().as_str().trim();
            let net_name = pin_cap.get(2).unwrap().as_str().trim();
            nets_mapping.insert(format!("{}/{}", inst, pin_name), net_name.to_string());
        }
    }

    // Derive the module name from the Verilog filename and locate the corresponding work directory
    let module_name = match verilog_file.file_stem() {
        Some(name) => name.to_string_lossy().into_owned(),
        None => {
            eprintln!("Failed to extract module name from file: {:?}", verilog_file);
            return;
        }
    };
    let work_dir = Path::new(&*tetramax_folder).join(&module_name);

    // Validate that the work directory exists, is a directory, and contains exactly six items.
    // If not, remove it (mirroring the Python `shutil.rmtree`) and skip processing.
    if !work_dir.exists() || !work_dir.is_dir() {
        return;
    }
    let entries = match fs::read_dir(&work_dir) {
        Ok(entries) => entries.collect::<Result<Vec<_>, _>>(),
        Err(e) => {
            eprintln!("Failed to list directory {:?}: {}", work_dir, e);
            return;
        }
    };
    if entries.as_ref().map_or(true, |v| v.len() != 6) {
        // Remove the directory on mismatch and return
        if let Err(e) = fs::remove_dir_all(&work_dir) {
            eprintln!("Failed to remove directory {:?}: {}", work_dir, e);
        }
        return;
    }

    // Process the `faults.txt` file if it exists.
    let faults_file_path = work_dir.join("faults.txt");
    if faults_file_path.exists() {
        if let Err(e) = process_file(&faults_file_path, &nets_mapping) {
            eprintln!("Failed to process faults.txt for {:?}: {}", verilog_file, e);
        }
    }

    // Process each CSV file under `simulation/bad`. Ensure the files are sorted to match Python's `sorted` behaviour.
    let bad_dir = work_dir.join("simulation/bad");
    if bad_dir.exists() && bad_dir.is_dir() {
        // Use glob to find all `.csv` files
        let pattern = bad_dir.join("*.csv").to_string_lossy().into_owned();
        let mut bad_files: Vec<PathBuf> = glob(&pattern)
            .expect("Failed to read glob pattern")
            .filter_map(Result::ok)
            .collect();
        bad_files.sort();
        for bad_file in bad_files {
            if let Err(e) = process_file(&bad_file, &nets_mapping) {
                eprintln!("Failed to process file {:?}: {}", bad_file, e);
            }
        }
    }
}

/// Process a single file: read, replace, and write back
fn process_file(
    file_path: &Path,
    nets_mapping: &HashMap<String, String>,
) -> Result<(), Box<dyn std::error::Error>> {
    // Open file in read-write mode
    let mut file = OpenOptions::new().read(true).write(true).open(file_path)?;

    // Read data
    let mut data = String::new();
    file.read_to_string(&mut data)?;

    // Preprocess data
    let output_data = preprocess_data(nets_mapping, &data)?;

    // Rewind and write back
    file.seek(SeekFrom::Start(0))?;
    file.write_all(output_data.as_bytes())?;
    file.set_len(output_data.len() as u64)?;

    Ok(())
}

/// Preprocess data: replace nets and remove duplicates
/// 
/// This mirrors the Python approach:
/// 1. Replace all instance/pin references with net names
/// 2. Parse each line into exactly 3 fields (split on whitespace, max 2 splits)
/// 3. Only keep lines with exactly 3 parts
/// 4. Remove duplicates while preserving order
/// 5. Join fields with "   " (3 spaces) and rows with "\n "
fn preprocess_data(
    nets_mapping: &HashMap<String, String>,
    data: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    let mut replaced_data = data.to_string();
    for (key, value) in nets_mapping {
        replaced_data = replaced_data.replace(key, value);
    }

    // Parse each line into exactly 3 fields, mimicking Python's `line.split(None, 2)`
    // This handles net names with spaces (e.g., "\div_6/u_div/BInv [24]")
    let mut records: Vec<[String; 3]> = Vec::new();
    for line in replaced_data.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        
        // Split on whitespace with max 2 splits to get exactly 3 fields
        let mut fields: Vec<&str> = Vec::with_capacity(3);
        let mut remaining = trimmed;
        
        // First split: find first whitespace
        if let Some(pos) = remaining.find(|c: char| c.is_whitespace()) {
            fields.push(&remaining[..pos]);
            remaining = remaining[pos..].trim_start();
        } else {
            continue; // Not enough fields
        }
        
        // Second split: find second whitespace
        if let Some(pos) = remaining.find(|c: char| c.is_whitespace()) {
            fields.push(&remaining[..pos]);
            remaining = remaining[pos..].trim_start();
        } else {
            continue; // Not enough fields
        }
        
        // Third field: everything remaining (may contain spaces)
        if !remaining.is_empty() {
            fields.push(remaining);
        } else {
            continue; // Not enough fields
        }
        
        // Only keep lines with exactly 3 parts
        if fields.len() == 3 {
            records.push([
                fields[0].to_string(),
                fields[1].to_string(),
                fields[2].to_string(),
            ]);
        }
    }

    // Remove duplicates while preserving original order (like pandas drop_duplicates)
    use std::collections::HashSet;
    let mut seen: HashSet<[String; 3]> = HashSet::new();
    let mut unique_records: Vec<[String; 3]> = Vec::new();
    for rec in records.into_iter() {
        if seen.insert(rec.clone()) {
            unique_records.push(rec);
        }
    }

    // Join fields with "   " (3 spaces) and rows with "\n "
    // Format: " field0   field1   field2\n field0   field1   field2..."
    let output_data = if unique_records.is_empty() {
        String::new()
    } else {
        let joined: Vec<String> = unique_records
            .iter()
            .map(|rec| rec.join("   "))
            .collect();
        format!(" {}", joined.join("\n "))
    };

    Ok(output_data)
}
