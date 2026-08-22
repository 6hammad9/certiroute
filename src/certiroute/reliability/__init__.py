"""Black-box, vendor-relative forecast calibration and evaluation."""

from certiroute.reliability.calibration import (
    InsufficientCalibrationSamplesError,
    build_vendor_relative_prediction_intervals,
    calibrate_horizon_conditioned_vendor_relative_intervals,
    calibrate_pooled_vendor_relative_intervals,
    evaluate_vendor_relative_intervals,
    finite_sample_absolute_residual_quantile,
    split_vendor_relative_pairs_by_target_time,
)
from certiroute.reliability.models import (
    FiniteSampleQuantile,
    HorizonBand,
    VendorRelativeConformalCalibration,
    VendorRelativeConformalGroup,
    VendorRelativeEvaluationMetrics,
    VendorRelativeForecastPair,
    VendorRelativePredictionInterval,
    VendorRelativeTemporalSplit,
)
from certiroute.reliability.thresholds import (
    CalibratedFutureTemperaturePoint,
    ForecastThresholdStatus,
    ThresholdCrossingPrediction,
    UnrealizedTemperatureForecastPoint,
    apply_vendor_relative_calibration_to_future_points,
    predict_future_threshold_crossing,
)

__all__ = [
    "CalibratedFutureTemperaturePoint",
    "FiniteSampleQuantile",
    "ForecastThresholdStatus",
    "HorizonBand",
    "InsufficientCalibrationSamplesError",
    "VendorRelativeConformalCalibration",
    "VendorRelativeConformalGroup",
    "VendorRelativeEvaluationMetrics",
    "VendorRelativeForecastPair",
    "VendorRelativePredictionInterval",
    "VendorRelativeTemporalSplit",
    "ThresholdCrossingPrediction",
    "UnrealizedTemperatureForecastPoint",
    "apply_vendor_relative_calibration_to_future_points",
    "build_vendor_relative_prediction_intervals",
    "calibrate_horizon_conditioned_vendor_relative_intervals",
    "calibrate_pooled_vendor_relative_intervals",
    "evaluate_vendor_relative_intervals",
    "finite_sample_absolute_residual_quantile",
    "predict_future_threshold_crossing",
    "split_vendor_relative_pairs_by_target_time",
]
