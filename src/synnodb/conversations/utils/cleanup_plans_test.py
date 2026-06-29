import json
import unittest

from synnodb.conversations.utils.cleanup_plans import (
    cleanup_duckdb_plan,
    cleanup_umbra_plan,
)


class TestCleanupPlans(unittest.TestCase):
    def test_cleanup_duckdbplan(self):
        plan = {
            "total_bytes_written": 0,
            "total_bytes_read": 0,
            "system_peak_temp_dir_size": 0,
            "system_peak_buffer_memory": 66779336192,
            "rows_returned": 1,
            "wal_replay_entry_count": 0,
            "result_set_size": 8,
            "query_name": "select\n    sum (l_extendedprice) / 7.0 as avg_yearly\nfrom\n    lineitem,\n    part\nwhere\n    p_partkey = l_partkey\n    and p_brand = 'Brand#54'\n    and p_container = 'MED PKG'\n    and l_quantity < (\n        select\n            0.2 * avg(l_quantity)\n        from\n            lineitem\n        where\n            l_partkey = p_partkey\n    );",
            "waiting_to_attach_latency": 0.0,
            "attach_load_storage_latency": 0.0,
            "blocked_thread_time": 0.0,
            "cpu_time": 1.5288956079995493,
            "extra_info": {},
            "total_memory_allocated": 14942208,
            "cumulative_cardinality": 121402720,
            "checkpoint_latency": 0.0,
            "cumulative_rows_scanned": 243989216,
            "write_to_wal_latency": 0.0,
            "latency": 1.581373988,
            "attach_replay_wal_latency": 0.0,
            "commit_local_storage_latency": 0.0,
            "children": [
                {
                    "system_peak_temp_dir_size": 0,
                    "system_peak_buffer_memory": 0,
                    "result_set_size": 8,
                    "operator_timing": 3.726e-06,
                    "operator_type": "PROJECTION",
                    "cpu_time": 1.5288926929995492,
                    "extra_info": {
                        "Projections": "avg_yearly",
                        "Estimated Cardinality": "1",
                    },
                    "cumulative_cardinality": 121402720,
                    "cumulative_rows_scanned": 243989216,
                    "operator_cardinality": 1,
                    "operator_name": "PROJECTION",
                    "operator_rows_scanned": 0,
                    "children": [
                        {
                            "system_peak_temp_dir_size": 0,
                            "system_peak_buffer_memory": 0,
                            "result_set_size": 16,
                            "operator_timing": 2.9643000000000007e-05,
                            "operator_type": "UNGROUPED_AGGREGATE",
                            "cpu_time": 1.5288889669995491,
                            "extra_info": {"Aggregates": "sum(#0)"},
                            "cumulative_cardinality": 121402719,
                            "cumulative_rows_scanned": 243989216,
                            "operator_cardinality": 1,
                            "operator_name": "UNGROUPED_AGGREGATE",
                            "operator_rows_scanned": 0,
                            "children": [
                                {
                                    "system_peak_temp_dir_size": 0,
                                    "system_peak_buffer_memory": 0,
                                    "result_set_size": 88000,
                                    "operator_timing": 5.598000000000002e-06,
                                    "operator_type": "PROJECTION",
                                    "cpu_time": 1.5288593239995492,
                                    "extra_info": {
                                        "Projections": "l_extendedprice",
                                        "Estimated Cardinality": "3330628",
                                    },
                                    "cumulative_cardinality": 121402718,
                                    "cumulative_rows_scanned": 243989216,
                                    "operator_cardinality": 11000,
                                    "operator_name": "PROJECTION",
                                    "operator_rows_scanned": 0,
                                    "children": [
                                        {
                                            "system_peak_temp_dir_size": 0,
                                            "system_peak_buffer_memory": 0,
                                            "result_set_size": 88000,
                                            "operator_timing": 7.691999999999997e-06,
                                            "operator_type": "PROJECTION",
                                            "cpu_time": 1.5288537259995492,
                                            "extra_info": {
                                                "Projections": "#2",
                                                "Estimated Cardinality": "3330628",
                                            },
                                            "cumulative_cardinality": 121391718,
                                            "cumulative_rows_scanned": 243989216,
                                            "operator_cardinality": 11000,
                                            "operator_name": "PROJECTION",
                                            "operator_rows_scanned": 0,
                                            "children": [
                                                {
                                                    "system_peak_temp_dir_size": 0,
                                                    "system_peak_buffer_memory": 0,
                                                    "result_set_size": 264000,
                                                    "operator_timing": 0.0013677429999999998,
                                                    "operator_type": "FILTER",
                                                    "cpu_time": 1.528846033999549,
                                                    "extra_info": {
                                                        "Expression": "(CAST(l_quantity AS DOUBLE) < SUBQUERY)",
                                                        "Estimated Cardinality": "3330628",
                                                    },
                                                    "cumulative_cardinality": 121380718,
                                                    "cumulative_rows_scanned": 243989216,
                                                    "operator_cardinality": 11000,
                                                    "operator_name": "FILTER",
                                                    "operator_rows_scanned": 0,
                                                    "children": [
                                                        {
                                                            "system_peak_temp_dir_size": 0,
                                                            "system_peak_buffer_memory": 0,
                                                            "result_set_size": 0,
                                                            "operator_timing": 0.004312392,
                                                            "operator_type": "RIGHT_DELIM_JOIN",
                                                            "cpu_time": 1.527478290999549,
                                                            "extra_info": {
                                                                "Join Type": "RIGHT",
                                                                "Conditions": "p_partkey IS NOT DISTINCT FROM p_partkey",
                                                                "Estimated Cardinality": "0",
                                                                "Delim Index": "1",
                                                            },
                                                            "cumulative_cardinality": 121369718,
                                                            "cumulative_rows_scanned": 243989216,
                                                            "operator_cardinality": 0,
                                                            "operator_name": "RIGHT_DELIM_JOIN",
                                                            "operator_rows_scanned": 0,
                                                            "children": [
                                                                {
                                                                    "system_peak_temp_dir_size": 0,
                                                                    "system_peak_buffer_memory": 0,
                                                                    "result_set_size": 3908000,
                                                                    "operator_timing": 0.12672496599996586,
                                                                    "operator_type": "HASH_JOIN",
                                                                    "cpu_time": 0.7055612259999674,
                                                                    "extra_info": {
                                                                        "Join Type": "INNER",
                                                                        "Conditions": "l_partkey = p_partkey",
                                                                        "Estimated Cardinality": "3330628",
                                                                    },
                                                                    "cumulative_cardinality": 870314,
                                                                    "cumulative_rows_scanned": 123994608,
                                                                    "operator_cardinality": 122125,
                                                                    "operator_name": "HASH_JOIN",
                                                                    "operator_rows_scanned": 0,
                                                                    "children": [
                                                                        {
                                                                            "system_peak_temp_dir_size": 0,
                                                                            "system_peak_buffer_memory": 0,
                                                                            "result_set_size": 17858496,
                                                                            "operator_timing": 0.4992283940000015,
                                                                            "operator_type": "TABLE_SCAN",
                                                                            "cpu_time": 0.4992283940000015,
                                                                            "extra_info": {
                                                                                "Table": "memory.main.lineitem",
                                                                                "Type": "Sequential Scan",
                                                                                "Projections": [
                                                                                    "l_partkey",
                                                                                    "l_quantity",
                                                                                    "l_extendedprice",
                                                                                ],
                                                                                "Dynamic Filters": "optional: l_partkey>=814 AND optional: l_partkey<=3998066 AND optional: l_partkey IN BF(p_partkey)",
                                                                                "Estimated Cardinality": "119994608",
                                                                            },
                                                                            "cumulative_cardinality": 744104,
                                                                            "cumulative_rows_scanned": 119994608,
                                                                            "operator_cardinality": 744104,
                                                                            "operator_name": "SEQ_SCAN",
                                                                            "operator_rows_scanned": 119994608,
                                                                            "children": [],
                                                                        },
                                                                        {
                                                                            "system_peak_temp_dir_size": 0,
                                                                            "system_peak_buffer_memory": 0,
                                                                            "result_set_size": 32680,
                                                                            "operator_timing": 0.07960786600000004,
                                                                            "operator_type": "TABLE_SCAN",
                                                                            "cpu_time": 0.07960786600000004,
                                                                            "extra_info": {
                                                                                "Table": "memory.main.part",
                                                                                "Type": "Sequential Scan",
                                                                                "Projections": "p_partkey",
                                                                                "Filters": [
                                                                                    "p_brand='Brand#54'",
                                                                                    "p_container='MED PKG'",
                                                                                ],
                                                                                "Estimated Cardinality": "108109",
                                                                            },
                                                                            "cumulative_cardinality": 4085,
                                                                            "cumulative_rows_scanned": 4000000,
                                                                            "operator_cardinality": 4085,
                                                                            "operator_name": "SEQ_SCAN",
                                                                            "operator_rows_scanned": 4000000,
                                                                            "children": [],
                                                                        },
                                                                    ],
                                                                },
                                                                {
                                                                    "system_peak_temp_dir_size": 0,
                                                                    "system_peak_buffer_memory": 0,
                                                                    "result_set_size": 2931000,
                                                                    "operator_timing": 0.0033235760000000004,
                                                                    "operator_type": "HASH_JOIN",
                                                                    "cpu_time": 0.8175893909995817,
                                                                    "extra_info": {
                                                                        "Join Type": "RIGHT",
                                                                        "Conditions": "p_partkey IS NOT DISTINCT FROM p_partkey",
                                                                        "Estimated Cardinality": "0",
                                                                    },
                                                                    "cumulative_cardinality": 120495319,
                                                                    "cumulative_rows_scanned": 119994608,
                                                                    "operator_cardinality": 122125,
                                                                    "operator_name": "HASH_JOIN",
                                                                    "operator_rows_scanned": 0,
                                                                    "children": [
                                                                        {
                                                                            "system_peak_temp_dir_size": 0,
                                                                            "system_peak_buffer_memory": 0,
                                                                            "result_set_size": 65360,
                                                                            "operator_timing": 1.0807e-05,
                                                                            "operator_type": "PROJECTION",
                                                                            "cpu_time": 0.8142658149995817,
                                                                            "extra_info": {
                                                                                "Projections": [
                                                                                    "(0.2 * avg(l_quantity))",
                                                                                    "p_partkey",
                                                                                ],
                                                                                "Estimated Cardinality": "34484597",
                                                                            },
                                                                            "cumulative_cardinality": 120373194,
                                                                            "cumulative_rows_scanned": 119994608,
                                                                            "operator_cardinality": 4085,
                                                                            "operator_name": "PROJECTION",
                                                                            "operator_rows_scanned": 0,
                                                                            "children": [
                                                                                {
                                                                                    "system_peak_temp_dir_size": 0,
                                                                                    "system_peak_buffer_memory": 0,
                                                                                    "result_set_size": 65360,
                                                                                    "operator_timing": 4.226e-06,
                                                                                    "operator_type": "PROJECTION",
                                                                                    "cpu_time": 0.8142550079995817,
                                                                                    "extra_info": {
                                                                                        "Projections": [
                                                                                            "__internal_decompress_integral_bigint(#0, 1)",
                                                                                            "#1",
                                                                                        ],
                                                                                        "Estimated Cardinality": "34484597",
                                                                                    },
                                                                                    "cumulative_cardinality": 120369109,
                                                                                    "cumulative_rows_scanned": 119994608,
                                                                                    "operator_cardinality": 4085,
                                                                                    "operator_name": "PROJECTION",
                                                                                    "operator_rows_scanned": 0,
                                                                                    "children": [
                                                                                        {
                                                                                            "system_peak_temp_dir_size": 0,
                                                                                            "system_peak_buffer_memory": 0,
                                                                                            "result_set_size": 49020,
                                                                                            "operator_timing": 0.0025354890000000006,
                                                                                            "operator_type": "HASH_GROUP_BY",
                                                                                            "cpu_time": 0.8142507819995817,
                                                                                            "extra_info": {
                                                                                                "Groups": "#0",
                                                                                                "Aggregates": "avg(#1)",
                                                                                                "Estimated Cardinality": "34484597",
                                                                                            },
                                                                                            "cumulative_cardinality": 120365024,
                                                                                            "cumulative_rows_scanned": 119994608,
                                                                                            "operator_cardinality": 4085,
                                                                                            "operator_name": "HASH_GROUP_BY",
                                                                                            "operator_rows_scanned": 0,
                                                                                            "children": [
                                                                                                {
                                                                                                    "system_peak_temp_dir_size": 0,
                                                                                                    "system_peak_buffer_memory": 0,
                                                                                                    "result_set_size": 1465500,
                                                                                                    "operator_timing": 2.2553000000000002e-05,
                                                                                                    "operator_type": "PROJECTION",
                                                                                                    "cpu_time": 0.8117152929995817,
                                                                                                    "extra_info": {
                                                                                                        "Projections": [
                                                                                                            "p_partkey",
                                                                                                            "l_quantity",
                                                                                                        ],
                                                                                                        "Estimated Cardinality": "68969195",
                                                                                                    },
                                                                                                    "cumulative_cardinality": 120360939,
                                                                                                    "cumulative_rows_scanned": 119994608,
                                                                                                    "operator_cardinality": 122125,
                                                                                                    "operator_name": "PROJECTION",
                                                                                                    "operator_rows_scanned": 0,
                                                                                                    "children": [
                                                                                                        {
                                                                                                            "system_peak_temp_dir_size": 0,
                                                                                                            "system_peak_buffer_memory": 0,
                                                                                                            "result_set_size": 2442500,
                                                                                                            "operator_timing": 0.00015458699999999995,
                                                                                                            "operator_type": "PROJECTION",
                                                                                                            "cpu_time": 0.8116927399995817,
                                                                                                            "extra_info": {
                                                                                                                "Projections": [
                                                                                                                    "__internal_compress_integral_uinteger(#0, 1)",
                                                                                                                    "#1",
                                                                                                                    "#2",
                                                                                                                ],
                                                                                                                "Estimated Cardinality": "68969195",
                                                                                                            },
                                                                                                            "cumulative_cardinality": 120238814,
                                                                                                            "cumulative_rows_scanned": 119994608,
                                                                                                            "operator_cardinality": 122125,
                                                                                                            "operator_name": "PROJECTION",
                                                                                                            "operator_rows_scanned": 0,
                                                                                                            "children": [
                                                                                                                {
                                                                                                                    "system_peak_temp_dir_size": 0,
                                                                                                                    "system_peak_buffer_memory": 0,
                                                                                                                    "result_set_size": 2931000,
                                                                                                                    "operator_timing": 0.7415180459995835,
                                                                                                                    "operator_type": "HASH_JOIN",
                                                                                                                    "cpu_time": 0.8115381529995817,
                                                                                                                    "extra_info": {
                                                                                                                        "Join Type": "INNER",
                                                                                                                        "Conditions": "l_partkey = p_partkey",
                                                                                                                        "Estimated Cardinality": "68969195",
                                                                                                                    },
                                                                                                                    "cumulative_cardinality": 120116689,
                                                                                                                    "cumulative_rows_scanned": 119994608,
                                                                                                                    "operator_cardinality": 122125,
                                                                                                                    "operator_name": "HASH_JOIN",
                                                                                                                    "operator_rows_scanned": 0,
                                                                                                                    "children": [
                                                                                                                        {
                                                                                                                            "system_peak_temp_dir_size": 0,
                                                                                                                            "system_peak_buffer_memory": 0,
                                                                                                                            "result_set_size": 1919913024,
                                                                                                                            "operator_timing": 0.07002010699999821,
                                                                                                                            "operator_type": "TABLE_SCAN",
                                                                                                                            "cpu_time": 0.07002010699999821,
                                                                                                                            "extra_info": {
                                                                                                                                "Table": "memory.main.lineitem",
                                                                                                                                "Type": "Sequential Scan",
                                                                                                                                "Projections": [
                                                                                                                                    "l_partkey",
                                                                                                                                    "l_quantity",
                                                                                                                                ],
                                                                                                                                "Dynamic Filters": "optional: l_partkey>=814 AND optional: l_partkey<=3998066",
                                                                                                                                "Estimated Cardinality": "119994608",
                                                                                                                            },
                                                                                                                            "cumulative_cardinality": 119994564,
                                                                                                                            "cumulative_rows_scanned": 119994608,
                                                                                                                            "operator_cardinality": 119994564,
                                                                                                                            "operator_name": "SEQ_SCAN",
                                                                                                                            "operator_rows_scanned": 119994608,
                                                                                                                            "children": [],
                                                                                                                        },
                                                                                                                        {
                                                                                                                            "system_peak_temp_dir_size": 0,
                                                                                                                            "system_peak_buffer_memory": 0,
                                                                                                                            "result_set_size": 0,
                                                                                                                            "operator_timing": 0.0,
                                                                                                                            "operator_type": "DELIM_SCAN",
                                                                                                                            "cpu_time": 0.0,
                                                                                                                            "extra_info": {
                                                                                                                                "Delim Index": "1",
                                                                                                                                "Estimated Cardinality": "2238674",
                                                                                                                            },
                                                                                                                            "cumulative_cardinality": 0,
                                                                                                                            "cumulative_rows_scanned": 0,
                                                                                                                            "operator_cardinality": 0,
                                                                                                                            "operator_name": "DELIM_SCAN",
                                                                                                                            "operator_rows_scanned": 0,
                                                                                                                            "children": [],
                                                                                                                        },
                                                                                                                    ],
                                                                                                                }
                                                                                                            ],
                                                                                                        }
                                                                                                    ],
                                                                                                }
                                                                                            ],
                                                                                        }
                                                                                    ],
                                                                                }
                                                                            ],
                                                                        },
                                                                        {
                                                                            "system_peak_temp_dir_size": 0,
                                                                            "system_peak_buffer_memory": 0,
                                                                            "result_set_size": 0,
                                                                            "operator_timing": 0.0,
                                                                            "operator_type": "DUMMY_SCAN",
                                                                            "cpu_time": 0.0,
                                                                            "extra_info": {},
                                                                            "cumulative_cardinality": 0,
                                                                            "cumulative_rows_scanned": 0,
                                                                            "operator_cardinality": 0,
                                                                            "operator_name": "DUMMY_SCAN",
                                                                            "operator_rows_scanned": 0,
                                                                            "children": [],
                                                                        },
                                                                    ],
                                                                },
                                                                {
                                                                    "system_peak_temp_dir_size": 0,
                                                                    "system_peak_buffer_memory": 0,
                                                                    "result_set_size": 32680,
                                                                    "operator_timing": 1.5282e-05,
                                                                    "operator_type": "HASH_GROUP_BY",
                                                                    "cpu_time": 1.5282e-05,
                                                                    "extra_info": {
                                                                        "Groups": "#0",
                                                                        "Aggregates": "",
                                                                        "Estimated Cardinality": "2238674",
                                                                    },
                                                                    "cumulative_cardinality": 4085,
                                                                    "cumulative_rows_scanned": 0,
                                                                    "operator_cardinality": 4085,
                                                                    "operator_name": "HASH_GROUP_BY",
                                                                    "operator_rows_scanned": 0,
                                                                    "children": [],
                                                                },
                                                            ],
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        cleaned_plan = cleanup_duckdb_plan(plan)

        # compute size reduction
        original_size = len(str(json.dumps(plan, indent=2)))
        cleaned_size = len(str(cleaned_plan))

        print(cleaned_plan)

        print(f"Original plan size: {original_size} characters")
        print(f"Cleaned plan size: {cleaned_size} characters")

    def test_cleanup_numeric_value(self):
        from synnodb.conversations.utils.cleanup_plans import cleanup_numeric_value

        self.assertEqual(cleanup_numeric_value(0.000123456), "0.00012")
        self.assertEqual(cleanup_numeric_value(123456), "120000")
        self.assertEqual(cleanup_numeric_value(0.00000000000123456), "0.0000000000012")
        self.assertEqual(cleanup_numeric_value(0.0), "0.0")
        self.assertEqual(cleanup_numeric_value(4.226e-06), "0.0000042")

    def test_cleanup_umbra_plan(self):
        plan = {
            "plan": {
                "operator": "groupby",
                "physicalOperator": "ungroupedaggregation",
                "cardinality": 1,
                "operatorId": 1,
                "analyzePlanId": 0,
                "analyzePlanCardinality": 1,
                "analyzePlanCounters": {},
                "input": {
                    "operator": "join",
                    "physicalOperator": "hashjoin",
                    "cardinality": 1866909,
                    "operatorId": 2,
                    "analyzePlanId": 1,
                    "analyzePlanCardinality": 11000,
                    "analyzePlanCounters": {
                        "slots": 8192,
                        "chains": 3255,
                        "minmaxbounds": "off",
                        "hashtable": "compact",
                        "filter": "off",
                        "chaining": "off",
                    },
                    "left": {
                        "operator": "map",
                        "physicalOperator": "map",
                        "cardinality": 4085,
                        "operatorId": 3,
                        "analyzePlanId": 2,
                        "analyzePlanCardinality": 4085,
                        "analyzePlanCounters": {},
                        "input": {
                            "operator": "groupjoin",
                            "physicalOperator": "hybridgroupjoin",
                            "cardinality": 4085,
                            "operatorId": 4,
                            "analyzePlanId": 3,
                            "analyzePlanCardinality": 4085,
                            "analyzePlanCounters": {},
                            "left": {
                                "operator": "earlyexecution",
                                "physicalOperator": "earlyexecution",
                                "cardinality": 4085,
                                "operatorId": 5,
                                "analyzePlanId": 4,
                                "analyzePlanCardinality": 4085,
                                "analyzePlanCounters": {},
                                "attributes": [
                                    {"name": "p_partkey", "iu": "p_partkey"}
                                ],
                                "restrictions": [],
                                "residuals": [],
                                "input": {
                                    "operator": "tablescan",
                                    "physicalOperator": "tablescan",
                                    "cardinality": 3906,
                                    "operatorId": 6,
                                    "analyzePlanId": 5,
                                    "analyzePlanCardinality": 0,
                                    "analyzePlanCounters": {},
                                    "attributes": [
                                        {"name": "p_partkey", "iu": "p_partkey2"},
                                        {"name": "p_name", "iu": "p_name"},
                                        {"name": "p_mfgr", "iu": "p_mfgr"},
                                        {"name": "p_brand", "iu": "p_brand"},
                                        {"name": "p_type", "iu": "p_type"},
                                        {"name": "p_size", "iu": "p_size"},
                                        {"name": "p_container", "iu": "p_container"},
                                        {
                                            "name": "p_retailprice",
                                            "iu": "p_retailprice",
                                        },
                                        {"name": "p_comment", "iu": "p_comment"},
                                    ],
                                    "restrictions": [
                                        {
                                            "attribute": 6,
                                            "mode": "=",
                                            "value": {
                                                "expression": "const",
                                                "value": {
                                                    "type": {
                                                        "type": "char",
                                                        "precision": 10,
                                                    },
                                                    "value": "MED PKG",
                                                },
                                            },
                                            "estimatedSelectivity": 0.015625,
                                            "collate": "",
                                        },
                                        {
                                            "attribute": 3,
                                            "mode": "=",
                                            "value": {
                                                "expression": "const",
                                                "value": {
                                                    "type": {
                                                        "type": "char",
                                                        "precision": 10,
                                                    },
                                                    "value": "Brand#54",
                                                },
                                            },
                                            "estimatedSelectivity": 0.04589825,
                                            "collate": "",
                                        },
                                    ],
                                    "residuals": [],
                                    "tid": "tid",
                                    "tableoid": "tableoid",
                                    "rowstate": "rowstate",
                                    "schema": "public",
                                    "table": {"type": "table", "id": 18},
                                    "tableSize": 4000000.0,
                                    "tablename": "part",
                                },
                                "mapping": {
                                    "mapping": [
                                        {"key": "p_partkey2", "value": "p_partkey"}
                                    ]
                                },
                                "result": 0,
                            },
                            "right": {
                                "operator": "tablescan",
                                "physicalOperator": "tablescan",
                                "cardinality": 119994608,
                                "operatorId": 7,
                                "analyzePlanId": 6,
                                "analyzePlanCardinality": 119994608,
                                "analyzePlanCounters": {},
                                "attributes": [
                                    {"name": "l_orderkey", "iu": "l_orderkey"},
                                    {"name": "l_partkey", "iu": "l_partkey"},
                                    {"name": "l_suppkey", "iu": "l_suppkey"},
                                    {"name": "l_linenumber", "iu": "l_linenumber"},
                                    {"name": "l_quantity", "iu": "l_quantity"},
                                    {
                                        "name": "l_extendedprice",
                                        "iu": "l_extendedprice",
                                    },
                                    {"name": "l_discount", "iu": "l_discount"},
                                    {"name": "l_tax", "iu": "l_tax"},
                                    {"name": "l_returnflag", "iu": "l_returnflag"},
                                    {"name": "l_linestatus", "iu": "l_linestatus"},
                                    {"name": "l_shipdate", "iu": "l_shipdate"},
                                    {"name": "l_commitdate", "iu": "l_commitdate"},
                                    {"name": "l_receiptdate", "iu": "l_receiptdate"},
                                    {"name": "l_shipinstruct", "iu": "l_shipinstruct"},
                                    {"name": "l_shipmode", "iu": "l_shipmode"},
                                    {"name": "l_comment", "iu": "l_comment"},
                                ],
                                "restrictions": [],
                                "residuals": [],
                                "tid": "tid30",
                                "tableoid": "tableoid31",
                                "rowstate": "rowstate32",
                                "schema": "public",
                                "table": {"type": "table", "id": 23},
                                "tableSize": 119994608,
                                "tablename": "lineitem",
                            },
                            "valuesLeft": [{"expression": "iuref", "iu": "p_partkey"}],
                            "valuesRight": [
                                {"expression": "iuref", "iu": "l_partkey"},
                                {"expression": "iuref", "iu": "l_quantity"},
                            ],
                            "keyLeft": [{"arg": 0, "iu": "p_partkey33", "collate": ""}],
                            "keyRight": [
                                {"arg": 0, "iu": "l_partkey34", "collate": ""}
                            ],
                            "compareTypes": [{"type": "integer"}],
                            "aggregatesLeft": [],
                            "aggregatesRight": [
                                {
                                    "op": "avg",
                                    "arg": 1,
                                    "collate": "",
                                    "iu": "avg(l_quantity)",
                                }
                            ],
                            "ordersLeft": [],
                            "ordersRight": [],
                            "behavior": "inner",
                            "forcedestimate": -1,
                        },
                        "values": [
                            {
                                "iu": "v",
                                "exp": {
                                    "expression": "mul",
                                    "left": {
                                        "expression": "const",
                                        "value": {
                                            "type": {
                                                "type": "bignumeric",
                                                "precision": 2,
                                                "scale": 1,
                                            },
                                            "value": 2,
                                            "value2": 0,
                                        },
                                    },
                                    "right": {
                                        "expression": "iuref",
                                        "iu": "avg(l_quantity)",
                                    },
                                },
                            }
                        ],
                    },
                    "right": {
                        "operator": "tablescan",
                        "physicalOperator": "tablescan",
                        "cardinality": 119994608,
                        "operatorId": 8,
                        "analyzePlanId": 7,
                        "analyzePlanCardinality": 119994608,
                        "analyzePlanCounters": {},
                        "attributes": [
                            {"name": "l_orderkey", "iu": "l_orderkey37"},
                            {"name": "l_partkey", "iu": "l_partkey38"},
                            {"name": "l_suppkey", "iu": "l_suppkey39"},
                            {"name": "l_linenumber", "iu": "l_linenumber40"},
                            {"name": "l_quantity", "iu": "l_quantity41"},
                            {"name": "l_extendedprice", "iu": "l_extendedprice42"},
                            {"name": "l_discount", "iu": "l_discount43"},
                            {"name": "l_tax", "iu": "l_tax44"},
                            {"name": "l_returnflag", "iu": "l_returnflag45"},
                            {"name": "l_linestatus", "iu": "l_linestatus46"},
                            {"name": "l_shipdate", "iu": "l_shipdate47"},
                            {"name": "l_commitdate", "iu": "l_commitdate48"},
                            {"name": "l_receiptdate", "iu": "l_receiptdate49"},
                            {"name": "l_shipinstruct", "iu": "l_shipinstruct50"},
                            {"name": "l_shipmode", "iu": "l_shipmode51"},
                            {"name": "l_comment", "iu": "l_comment52"},
                        ],
                        "restrictions": [],
                        "residuals": [],
                        "tid": "tid53",
                        "tableoid": "tableoid54",
                        "rowstate": "rowstate55",
                        "schema": "public",
                        "table": {"type": "table", "id": 23},
                        "tableSize": 119994608,
                        "tablename": "lineitem",
                    },
                    "condition": {
                        "expression": "and",
                        "input": [
                            {
                                "expression": "compare",
                                "left": {"expression": "iuref", "iu": "l_partkey34"},
                                "right": {"expression": "iuref", "iu": "l_partkey38"},
                                "direction": "=",
                                "collate": "",
                            },
                            {
                                "expression": "compare",
                                "left": {"expression": "iuref", "iu": "l_quantity41"},
                                "right": {"expression": "iuref", "iu": "v"},
                                "direction": "<",
                                "collate": "",
                            },
                        ],
                    },
                    "type": "inner",
                    "forcedestimate": 1866908.9170769532,
                },
                "values": [{"expression": "iuref", "iu": "l_extendedprice42"}],
                "key": [],
                "groupingmode": "static",
                "aggregates": [
                    {"op": "sum", "arg": 0, "collate": "", "iu": "sum(l_extendedprice)"}
                ],
                "orders": [],
                "groupingsets": [],
            },
            "ius": [
                {"iu": "l_orderkey37", "type": {"type": "integer"}},
                {"iu": "l_partkey38", "type": {"type": "integer"}},
                {"iu": "l_suppkey39", "type": {"type": "integer"}},
                {"iu": "l_linenumber40", "type": {"type": "integer"}},
                {
                    "iu": "l_quantity41",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_extendedprice42",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_discount43",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_tax44",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {"iu": "l_returnflag45", "type": {"type": "char1"}},
                {"iu": "l_linestatus46", "type": {"type": "char1"}},
                {"iu": "l_shipdate47", "type": {"type": "date"}},
                {"iu": "l_commitdate48", "type": {"type": "date"}},
                {"iu": "l_receiptdate49", "type": {"type": "date"}},
                {"iu": "l_shipinstruct50", "type": {"type": "char", "precision": 25}},
                {"iu": "l_shipmode51", "type": {"type": "char", "precision": 10}},
                {"iu": "l_comment52", "type": {"type": "text", "precision": 44}},
                {"iu": "tid53", "type": {"type": "bigint"}},
                {"iu": "tableoid54", "type": {"type": "integer"}},
                {"iu": "rowstate55", "type": {"type": "bigint"}},
                {"iu": "p_partkey2", "type": {"type": "integer"}},
                {"iu": "p_name", "type": {"type": "text", "precision": 55}},
                {"iu": "p_mfgr", "type": {"type": "char", "precision": 25}},
                {"iu": "p_brand", "type": {"type": "char", "precision": 10}},
                {"iu": "p_type", "type": {"type": "text", "precision": 25}},
                {"iu": "p_size", "type": {"type": "integer"}},
                {"iu": "p_container", "type": {"type": "char", "precision": 10}},
                {
                    "iu": "p_retailprice",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {"iu": "p_comment", "type": {"type": "text", "precision": 23}},
                {"iu": "tid", "type": {"type": "bigint"}},
                {"iu": "tableoid", "type": {"type": "integer"}},
                {"iu": "rowstate", "type": {"type": "bigint"}},
                {"iu": "l_orderkey", "type": {"type": "integer"}},
                {"iu": "l_partkey", "type": {"type": "integer"}},
                {"iu": "l_suppkey", "type": {"type": "integer"}},
                {"iu": "l_linenumber", "type": {"type": "integer"}},
                {
                    "iu": "l_quantity",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_extendedprice",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_discount",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {
                    "iu": "l_tax",
                    "type": {"type": "numeric", "precision": 15, "scale": 2},
                },
                {"iu": "l_returnflag", "type": {"type": "char1"}},
                {"iu": "l_linestatus", "type": {"type": "char1"}},
                {"iu": "l_shipdate", "type": {"type": "date"}},
                {"iu": "l_commitdate", "type": {"type": "date"}},
                {"iu": "l_receiptdate", "type": {"type": "date"}},
                {"iu": "l_shipinstruct", "type": {"type": "char", "precision": 25}},
                {"iu": "l_shipmode", "type": {"type": "char", "precision": 10}},
                {"iu": "l_comment", "type": {"type": "text", "precision": 44}},
                {"iu": "tid30", "type": {"type": "bigint"}},
                {"iu": "tableoid31", "type": {"type": "integer"}},
                {"iu": "rowstate32", "type": {"type": "bigint"}},
                {
                    "iu": "avg(l_quantity)",
                    "type": {"type": "bignumeric", "precision": 34, "scale": 21},
                },
                {
                    "iu": "sum(l_extendedprice)",
                    "type": {
                        "type": "numeric",
                        "precision": 18,
                        "scale": 2,
                        "nullable": True,
                    },
                },
                {"iu": "l_partkey34", "type": {"type": "integer"}},
                {"iu": "p_partkey", "type": {"type": "integer"}},
                {"iu": "p_partkey33", "type": {"type": "integer"}},
                {
                    "iu": "v",
                    "type": {"type": "bignumeric", "precision": 36, "scale": 22},
                },
            ],
            "output": [
                {
                    "name": "avg_yearly",
                    "iu": {
                        "expression": "div",
                        "left": {"expression": "iuref", "iu": "sum(l_extendedprice)"},
                        "right": {
                            "expression": "const",
                            "value": {
                                "type": {"type": "numeric", "precision": 2, "scale": 1},
                                "value": 70,
                            },
                        },
                    },
                }
            ],
            "type": "select",
            "query": True,
            "analyzePlanPipelines": [
                {
                    "pipelineId": 0,
                    "start": 14,
                    "stop": 75,
                    "duration": 61,
                    "parallelism": "multi-threaded",
                    "operators": [4],
                    "counters": {},
                },
                {
                    "pipelineId": 1,
                    "start": 75,
                    "stop": 292727,
                    "duration": 292652,
                    "parallelism": "multi-threaded",
                    "operators": [6],
                    "counters": {},
                },
                {
                    "pipelineId": 2,
                    "start": 292729,
                    "stop": 292847,
                    "duration": 118,
                    "parallelism": "multi-threaded",
                    "operators": [3, 2],
                    "counters": {},
                },
                {
                    "pipelineId": 3,
                    "start": 292847,
                    "stop": 651644,
                    "duration": 358797,
                    "parallelism": "multi-threaded",
                    "operators": [7, 1],
                    "counters": {},
                },
                {
                    "pipelineId": 4,
                    "start": 651646,
                    "stop": 651648,
                    "duration": 2,
                    "parallelism": "single-threaded",
                    "operators": [0],
                    "counters": {},
                },
            ],
        }

        cleaned_plan = cleanup_umbra_plan(plan)
        # print(cleaned_plan)

        print(f"Original plan size: {len(str(json.dumps(plan, indent=2)))} characters")
        print(f"Cleaned plan size: {len(str(cleaned_plan))} characters")


if __name__ == "__main__":
    unittest.main()
