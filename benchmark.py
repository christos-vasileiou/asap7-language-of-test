import timeit
import rust_math

# 1. Pure Python implementation (mirroring the Rust logic exactly)
def python_fibonacci(n):
    if n <= 1:
        return n
    
    a = 0
    b = 1
    
    for _ in range(2, n + 1):
        c = a + b
        a = b
        b = c
        
    return c

# 2. Benchmark settings
# We'll calculate the 90th Fibonacci number 1 million times.
N = 100
ITERATIONS = 1_000_000

print(f"Calculating the {N}th Fibonacci number {ITERATIONS:,} times...\n")

# 3. Time the Pure Python version
print("Running pure Python...")
py_time = timeit.timeit(lambda: python_fibonacci(N), number=ITERATIONS)
print(f"Python time: {py_time:.4f} seconds\n")

# 4. Time the Rust version
print("Running Rust module...")
rust_time = timeit.timeit(lambda: rust_math.calculate_fibonacci(N), number=ITERATIONS)
print(f"Rust time:   {rust_time:.4f} seconds\n")

# 5. Calculate the speedup
speedup = py_time / rust_time
print("-" * 30)
print(f"Result: Rust is {speedup:.2f}x faster!")
print("-" * 30)