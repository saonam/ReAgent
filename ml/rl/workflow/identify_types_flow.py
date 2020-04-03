#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

from typing import Dict, List, Optional

import pyspark
from ml.rl.preprocessing import normalization
from ml.rl.workflow.types import PreprocessingOptions, TableSpec
from pyspark.sql.functions import col, collect_list, explode, flatten


LOCAL_MASTER = "local[1]"


def get_spark_session(master: str = LOCAL_MASTER):
    """ Get a spark session """
    spark = (
        pyspark.sql.SparkSession.builder.master(master)
        .enableHiveSupport()
        .getOrCreate()
    )
    return spark


def normalization_helper(
    max_unique_enum_values: int,
    quantile_size: int,
    quantile_k2_threshold: float,
    skip_box_cox: bool = False,
    skip_quantiles: bool = False,
    feature_overrides: Optional[Dict[int, str]] = None,
    whitelist_features: Optional[List[int]] = None,
    assert_whitelist_feature_coverage: bool = True,
):
    """ Construct a preprocessing closure to obtain normalization parameters
    from rows of feature_name and a sample of feature_values.
    """

    norm_params = {
        "max_unique_enum_values": max_unique_enum_values,
        "quantile_size": quantile_size,
        "quantile_k2_threshold": quantile_k2_threshold,
        "skip_box_cox": skip_box_cox,
        "skip_quantiles": skip_quantiles,
        "feature_overrides": feature_overrides,
    }
    whitelist_features = set(whitelist_features or [])

    def validate_whitelist_features(
        params: Dict[int, normalization.NormalizationParameters],
    ) -> None:
        if not whitelist_features:
            return
        whitelist_feature_set = {int(fid) for fid in whitelist_features}
        available_features = set(params.keys())
        assert whitelist_feature_set == available_features, (
            "Could not identify preprocessing type for these features: {}; "
            "extra features: {}".format(
                whitelist_feature_set - available_features,
                available_features - whitelist_feature_set,
            )
        )

    def process(rows: List) -> Dict[int, normalization.NormalizationParameters]:
        params = {}
        for row in rows:
            assert "feature_name" in row
            assert "feature_values" in row
            norm_metdata = normalization.get_feature_norm_metadata(
                row["feature_name"], row["feature_values"], norm_params
            )
            if norm_metdata is not None and (
                not whitelist_features or row["feature_name"] in whitelist_features
            ):
                params[row["feature_name"]] = norm_metdata

        if assert_whitelist_feature_coverage:
            validate_whitelist_features(params)
        return params

    return process


def identify_normalization_parameters(
    table_spec: TableSpec,
    column_name: str,
    preprocessing_options: PreprocessingOptions,
    seed: int,
) -> Dict[int, normalization.NormalizationParameters]:
    """ Get normalization parameters """

    spark = get_spark_session()
    df = spark.sql(f"SELECT * FROM {table_spec.table_name}")
    df = create_normalization_spec_spark(
        df, column_name, preprocessing_options.num_samples, seed
    )
    rows = df.collect()
    spark.stop()

    normalization_processor = normalization_helper(
        max_unique_enum_values=preprocessing_options.max_unique_enum_values,
        quantile_size=preprocessing_options.quantile_size,
        quantile_k2_threshold=preprocessing_options.quantile_k2_threshold,
        skip_box_cox=preprocessing_options.skip_box_cox,
        skip_quantiles=preprocessing_options.skip_quantiles,
        feature_overrides=preprocessing_options.feature_overrides,
        whitelist_features=preprocessing_options.whitelist_features,
        assert_whitelist_feature_coverage=preprocessing_options.assert_whitelist_feature_coverage,
    )
    return normalization_processor(rows)


def create_normalization_spec_spark(df, column, num_samples: int, seed: int):
    """Returns approximately num_samples random rows from column of df."""

    df = df.select(
        explode(col(column).alias("features")).alias("feature_name", "feature_value")
    )

    # calculate fractions
    counts_df = df.groupBy("feature_name").count()
    frac = {}
    for row in counts_df.collect():
        assert num_samples <= row["count"]
        frac[row["feature_name"]] = num_samples / row["count"]

    # TODO(T64843081): change to reservoir sampling, currently it approximates
    # perform sampling and collect them
    df = df.sampleBy("feature_name", fractions=frac, seed=seed)
    df = df.groupBy("feature_name").agg(
        collect_list("feature_value").alias("feature_value_list")
    )
    df = df.select(
        "feature_name", flatten("feature_value_list").alias("feature_values")
    )
    return df
