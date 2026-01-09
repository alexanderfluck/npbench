import argparse
import traceback
import subprocess
import re
import copy
import os
import importlib

from datetime import datetime, timezone
from collections import defaultdict
from math import sqrt
from statistics import median

import dace
from dace.config import Config
from dace.codegen.instrumentation import papi
import dace.sdfg.performance_evaluation.work_depth_copy as wd


from npbench.infrastructure import (Benchmark, utilities as util, DaceFramework)

#################### SQL for creating tables and inserting values ##################################
event_averages_table_sql = """
CREATE TABLE IF NOT EXISTS event_averages(
    collection_script_timestamp integer NOT NULL,
    repetitions integer NOT NULL,
    benchmark text NOT NULL,
    preset text NOT NULL,
    event_name text NOT NULL,
    average real NOT NULL,
    median integer NOT NULL,
    variance real NOT NULL,
    standard_dev real NOT NULL,
    standard_dev_percent real NOT NULL,
    time real,
    PRIMARY KEY (collection_script_timestamp, benchmark, event_name)
);
"""
insert_into_averages_table_sql = """
INSERT INTO event_averages(
    collection_script_timestamp, repetitions, benchmark, preset, event_name, average, median, variance,
    standard_dev, standard_dev_percent, time
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


event_counts_table_sql = """
CREATE TABLE IF NOT EXISTS event_counts(
    report_timestamp integer NOT NULL,
    report_path text NOT NULL,
    collection_script_timestamp integer NOT NULL,
    benchmark text NOT NULL,
    preset text NOT NULL,
    event text NOT NULL,
    total_count integer NOT NULL,
    time real NOT NULL,
    PRIMARY KEY (report_timestamp, event)
);
"""
insert_into_event_table_sql = """
INSERT INTO event_counts(
    report_timestamp, report_path, collection_script_timestamp, benchmark, preset, event, total_count, time
) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""
############################################ SQL end ############################################################


######################################## Helper Functions #######################################################

def get_bench_sdfg(bench:Benchmark, dace_framework:DaceFramework):
        module_pypath = "npbench.benchmarks.{r}.{m}".format(r=bench.info["relative_path"].replace('/', '.'),
                                                            m=bench.info["module_name"])
        if "postfix" in dace_framework.info.keys():
            postfix = dace_framework.info["postfix"]
        else:
            postfix = dace_framework.fname
        module_str = "{m}_{p}".format(m=module_pypath, p=postfix)
        func_str = bench.info["func_name"]

        ldict = dict()
        # Import DaCe implementation
        try:
            module = importlib.import_module(module_str)
            ct_impl = getattr(module, func_str)

        except Exception as e:
            print("Failed to load the DaCe implementation.")
            raise (e)

        ##### Experimental: Load strict SDFG
        sdfg_loaded = False
        if dace_framework.load_strict:
            path = os.path.join(os.getcwd(), 'dace_sdfgs', f"{module_str}-{func_str}.sdfg")
            try:
                strict_sdfg = dace.SDFG.from_file(path)
                sdfg_loaded = True
            except Exception:
                pass

        if not sdfg_loaded:
            #########################################################
            # Prepare SDFGs
            base_sdfg, _ = util.benchmark("__npb_result = ct_impl.to_sdfg(simplify=False)",
                                                   out_text="DaCe parsing time",
                                                   context=locals(),
                                                   output='__npb_result',
                                                   verbose=False)
            strict_sdfg = copy.deepcopy(base_sdfg)
            strict_sdfg._name = "strict"
            ldict['strict_sdfg'] = strict_sdfg
            simplified_sdfg, _ = util.benchmark("strict_sdfg.simplify()",
                                            out_text="DaCe Strict Transformations time",
                                            context=locals(),
                                            verbose=False)
            # sdfg_list = [strict_sdfg]
            # time_list = [parse_time[0] + strict_time[0]]
        else:
            ldict['strict_sdfg'] = strict_sdfg

        ##### Experimental: Saving strict SDFG
        if dace_framework.save_strict and not sdfg_loaded:
            path = os.path.join(os.getcwd(), 'dace_sdfgs')
            try:
                os.mkdir(path)
            except FileExistsError:
                pass
            path = os.path.join(os.getcwd(), 'dace_sdfgs', f"{module_str}-{func_str}.sdfg")
            strict_sdfg.save(path)

        return base_sdfg, simplified_sdfg

def get_availaple_papi_events():
    """
    Extract available (Avail == Yes) PAPI preset event names
    from papi_avail output.
    """
    events = []

    result = subprocess.run(
        ["papi_avail"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    output = result.stdout
    for line in output.splitlines():
        line = line.strip()

        # Capture: name, code, availability
        match = re.match(
            r"^(PAPI_[A-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+(Yes|No)\b",
            line,
        )
        if match and match.group(2) == "Yes":
            events.append(match.group(1))

    return set(events)

def get_native_papi_events():
    result = subprocess.run(
        ["papi_native_avail"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    output = result.stdout
    events: dict[str, list[str]] = defaultdict(list)
    current_event = None

    event_header_re = re.compile(r"^\|\s*([A-Za-z0-9_:.-]+)\s*\|$")
    modifier_re = re.compile(r"^\|\s*:([A-Za-z0-9_]+)")

    for line in output.splitlines():
        line = line.rstrip()

        # Match event header
        header_match = event_header_re.match(line)
        if header_match:
            current_event = header_match.group(1)
            events[current_event] = []
            continue

        # Match modifiers within an event block
        if current_event:
            mod_match = modifier_re.match(line)
            if mod_match:
                modifier = mod_match.group(1)
                events[current_event].append(modifier)

    return dict(events)

def papi_addable_events(current: list[str]|set[str]) -> set[str]:
    """
    Return preset events that can be added to the given event set.
    """
    proc = subprocess.run(
        ["papi_event_chooser", "PRESET", *current],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    addable = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("PAPI_"):
            event = line.split(None, 1)[0]
            addable.add(event)
    return addable

def build_papi_event_sets(events:set[str]|list[str]):
    remaining = set(events)
    event_sets = []

    while remaining:
        x = next(iter(remaining))
        current = [x]

        while True:
            addable = papi_addable_events(current)
            candidates = addable & remaining

            if not candidates:
                break

            y = next(iter(candidates))
            current.append(y)

        event_sets.append(current)
        remaining -= set(current)

    return event_sets

######################################## Helper Functions end ###################################################

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-p",
                        "--preset",
                        choices=['S', 'M', 'L', 'paper'],
                        nargs="?",
                        default='S')
    parser.add_argument("-v",
                        "--validate",
                        type=util.str2bool,
                        nargs="?",
                        default=True)
    parser.add_argument("-e",
                        "--build_event_sets",
                        type=util.str2bool,
                        nargs="?",
                        default=True)
    parser.add_argument("-r", "--repeat", type=int, nargs="?", default=10)
    parser.add_argument("-b", "--benchmarks", type=str, nargs="+", default=None)

    args = vars(parser.parse_args())

    benchmark_set = ['adi','arc_distance','atax','azimint_hist','azimint_naive','bicg',
                  'cavity_flow','channel_flow','cholesky2','cholesky','compute','contour_integral',
                  'conv2d_bias','correlation',#'covariance2',
                  'covariance','crc16','deriche',#'doitgen',
                  'durbin','fdtd_2d','floyd_warshall','gemm','gemver','gesummv','go_fast','gramschmidt',
                  'hdiff','heat_3d','jacobi_1d','jacobi_2d','k2mm','k3mm','lenet','ludcmp','lu','mandelbrot1',
                  'mandelbrot2','mlp','mvt','nbody','nussinov','resnet','scattering_self_energies','seidel_2d',
                  'softmax','spmv',#'stockham_fft',
                  'symm','syr2k','syrk','trisolv','trmm','vadv']
    
    benchmarks = list()
    if args["benchmarks"]:
        requested_bms = []
        for benchmark_name in args["benchmarks"]:
            if benchmark_name in benchmark_set:
                benchmarks.append(benchmark_name)
            else:
                print(f"Could not find a benchmark with the name {benchmark_name}")
    else:
        benchmarks = list(benchmark_set)
    

    dace_cpu_framework = DaceFramework("dace_cpu")
    repetitions = args["repeat"]
    preset = args["preset"]

    # create a database connection
    database = r"npbench_papi_metrics.db"
    conn = util.create_connection(database)
    run_id = int(datetime.now(timezone.utc).timestamp() * 1000)
    available_papi_events = get_availaple_papi_events()
    native_papi_events = get_native_papi_events()

    fp_events = ["PAPI_FP_OPS", "PAPI_DP_OPS"]
    cache_events = ['PAPI_L1_DCM', 'PAPI_L1_ICM', 'PAPI_L2_DCM', 'PAPI_L2_ICM', 'PAPI_L3_DCM', 
                    'PAPI_L3_ICM', 'PAPI_L1_TCM', 'PAPI_L2_TCM', 'PAPI_L3_TCM', 'PAPI_CA_SNP', 
                    'PAPI_CA_SHR', 'PAPI_CA_CLN', 'PAPI_CA_INV', 'PAPI_CA_ITV', 'PAPI_L3_LDM', 
                    'PAPI_L3_STM', 'PAPI_L1_LDM', 'PAPI_L1_STM', 'PAPI_L2_LDM', 'PAPI_L2_STM',
                    'PAPI_PRF_DM', 'PAPI_L3_DCH', 'PAPI_LD_INS', 'PAPI_SR_INS', 'PAPI_LST_INS',
                    'PAPI_L1_DCH', 'PAPI_L2_DCH', 'PAPI_L1_DCA', 'PAPI_L2_DCA', 'PAPI_L3_DCA', 
                    'PAPI_L1_DCR', 'PAPI_L2_DCR', 'PAPI_L3_DCR', 'PAPI_L1_DCW', 'PAPI_L2_DCW', 
                    'PAPI_L3_DCW', 'PAPI_L1_ICH', 'PAPI_L2_ICH', 'PAPI_L3_ICH', 'PAPI_L1_ICA', 
                    'PAPI_L2_ICA', 'PAPI_L3_ICA', 'PAPI_L1_ICR', 'PAPI_L2_ICR', 'PAPI_L3_ICR', 
                    'PAPI_L1_ICW', 'PAPI_L2_ICW', 'PAPI_L3_ICW', 'PAPI_L1_TCH', 'PAPI_L2_TCH', 
                    'PAPI_L3_TCH', 'PAPI_L1_TCA', 'PAPI_L2_TCA', 'PAPI_L3_TCA', 'PAPI_L1_TCR', 
                    'PAPI_L2_TCR', 'PAPI_L3_TCR', 'PAPI_L1_TCW', 'PAPI_L2_TCW', 'PAPI_L3_TCW']

    fp_event = ""
    for event in fp_events:
        if event in available_papi_events:
            fp_event = event
            break
    available_cache_events = []
    for event in cache_events:
        if event in available_papi_events:
            available_cache_events.append(event)
    event_sets = [{fp_event}]
    if args["build_event_sets"]:
        event_lists = build_papi_event_sets(available_cache_events)
        #event_sets.extend([set(l) for l in event_lists])
    else:
        event_sets.extend([{e} for e in available_cache_events])

    util.create_table(conn=conn, create_table_sql=event_averages_table_sql)
    first_bench = True

    for benchmark_name in benchmarks:
        print("="*50, benchmark_name, "="*50)
        benchmark = Benchmark(benchmark_name)
        
        for event_set in event_sets:
            if first_bench:
                util.create_table(conn=conn, create_table_sql=event_counts_table_sql)

            try:

                sdfg, simplified_sdfg = get_bench_sdfg(benchmark, dace_cpu_framework)
                bdata = benchmark.get_data(args["preset"])

                sdfg.compile()
                benchmark.info["parameters"].keys()
                substitutions = benchmark.info["parameters"][preset]
                print(substitutions)
                work, depth = wd.analyze_sdfg(sdfg, {}, wd.get_tasklet_work_depth, [])
                work = work.subs(substitutions)
                depth = depth.subs(substitutions)
                print(work, depth)
        
                Config.set("compiler", "cpu", "args", value="-O0")
                Config.set("instrumentation","papi", "default_counters", value=str(list(event_set)))   
                
                sdfg.instrument = dace.InstrumentationType.PAPI_Counters

                c_sdfg = sdfg.compile()

                time_list = []
                for _ in range(repetitions):
                    run_bdata = copy.deepcopy(bdata)
                    #warmup
                    c_sdfg(**run_bdata)
                    #measured run
                    _, raw_time_list = util.benchmark("c_sdfg(**run_bdata)", context=locals(), verbose=False, repeat=1)
                    time_list.extend(raw_time_list)
                
                new_reports = sorted(sdfg.get_instrumentation_reports(), key=lambda report: report.name, reverse=True)[0:repetitions]

                event_sums = dict()
                for i, report in enumerate(reversed(new_reports)):
                    for uuid in report.counters:
                        for sdfg_element in report.counters[uuid]:
                            for event_name in report.counters[uuid][sdfg_element]:
                                event_sum =  0
                                for tid in report.counters[uuid][sdfg_element][event_name]:
                                    event_sum+=report.counters[uuid][sdfg_element][event_name][tid][0]
                                
                                if event_name not in event_sums.keys():
                                    event_sums[event_name] = []
                                
                                event_sums[event_name].append(event_sum)
                                util.create_result(conn, insert_into_event_table_sql, tuple([int(report.name), str(report.filepath), run_id, benchmark_name, preset, event_name, event_sum, time_list[i]]))
                for event, sums in event_sums.items():
                    event_average = sum(sums)/repetitions
                    event_median = median(sums)
                    event_variance = sum([(counter_value-event_average)**2 for counter_value in sums])/repetitions
                    event_stddev = sqrt(event_variance)
                    time_average = sum(time_list)/repetitions
                    stddev_perc = event_stddev/(event_average)*100 if event_average>0 else -1
                    print(
                        f"{event:<14} | "
                        f"Max: {max(sums):>16.4f} | "
                        f"Min: {min(sums):>16.4f} | "
                        f"Avg: {event_average:>16.4f} | "
                        f"Var: {event_variance:>20.4f} | "
                        f"StdDev: {event_stddev:>16.4f} | "
                        f"StdDev%: {stddev_perc:>16.4f}%"
                    )
                    util.create_result(conn, insert_into_averages_table_sql, tuple([run_id, repetitions, benchmark_name, preset, event, event_average, event_median, event_variance, event_stddev, stddev_perc, time_average]))
                    print("Difference: ", abs(event_average-work))
            except Exception as e:
                print(e)
                traceback.print_exc()
                continue 
        first_bench = False
    end = (int(datetime.now(timezone.utc).timestamp() * 1000))
    diration = (end - run_id)
    print("Duration:",  (end - run_id)/(1000*60), "min")