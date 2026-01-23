# Fault Net Preprocessing in Rust

An implementation of the fault net preprocessing script, originally written in Python, now rewritten in Rust for improved performance and concurrency.

## Table of Contents

- [Fault Net Preprocessing in Rust](#fault-net-preprocessing-in-rust)
  - [Table of Contents](#table-of-contents)
  - [Introduction](#introduction)
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)

## Introduction

This project provides a Rust implementation of a fault net preprocessing script used for processing Verilog files and associated fault data. The script reads Verilog files, processes fault data, replaces net names based on mappings, and handles multiple files in parallel using efficient concurrency.

## Features

- **Efficient File Processing:** Quickly processes large numbers of Verilog and fault files.
- **Parallel Execution:** Utilizes multi-threading for faster processing using the Rayon crate.
- **Progress Indication:** Displays a progress bar to monitor the processing status.
- **Regex Parsing:** Uses regular expressions to parse and extract data from Verilog files.
- **Command-Line Interface:** Provides an easy-to-use CLI for specifying input and output directories.

## Prerequisites

- **Rust**: Ensure you have Rust installed. If not, install it from [rust-lang.org](https://www.rust-lang.org/tools/install).

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/yourusername/fault-net-preprocessing-rust.git
