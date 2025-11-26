import psutil
from cpuinfo import get_cpu_info
import GPUtil
import subprocess
import re

def get_cpu_flops():
    cpu_info = get_cpu_info()
    hw_threads = psutil.cpu_count(logical=False)
    clock_speed = psutil.cpu_freq().max / 1000  
    # Guess SIMD width
    flags = cpu_info.get('flags', [])
    if 'avx512f' in flags:
        simd_width = 512
    elif 'avx2' in flags:
        simd_width = 256
    elif 'avx' in flags:
        simd_width = 256
    elif 'sse' in flags:
        simd_width = 128
    else:
        simd_width = 64

    elements_per_vector = simd_width / 64
    flops_per_cycle = elements_per_vector * 2  # with FMA

    gflops = hw_threads * clock_speed * 1e9 * flops_per_cycle / 1e9
    return gflops


def get_gpu_flops():
    gpus = GPUtil.getGPUs()
    results = []
    for gpu in gpus:
        name = gpu.name
        cuda_cores = None
        # Try to infer CUDA cores count from GPU name if available (for NVIDIA)
        match = re.search(r'(\d{3,5})', gpu.name)
        if match:
            cuda_cores = int(match.group(1))

        # Use nvidia-smi to get more accurate info if possible
        try:
            smi_output = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=name,clocks.max.sm,clocks.current.sm', '--format=csv,noheader'],
                encoding='utf-8'
            )
            clock_speed = float(re.findall(r'(\d+)', smi_output.split(',')[1])[0]) / 1000  # GHz
        except Exception:
            clock_speed = gpu.clock / 1000  # fallback

        # Estimate FLOPs if CUDA core count known
        if cuda_cores:
            tflops = cuda_cores * clock_speed * 1e9 / 1e12
        else:
            tflops = None

        results.append({
            "name": name,
            "clock (GHz)": clock_speed,
            "CUDA cores": cuda_cores,
            "TFLOPs (FP32)": tflops
        })
    return results

if __name__ == "__main__":
    print("CPU flops:", get_cpu_flops(), "GFLOPs/s")
    for gpu in get_gpu_flops():
        print(f"{gpu["name"]} flops: {gpu["TFLOPs (FP32)"]} TFLOPs/s (FP32)")