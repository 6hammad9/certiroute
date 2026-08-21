from certiroute.fortyguard.results import extract_temperature_stats


def test_extract_temperature_stats_tolerates_documented_capitalization() -> None:
    stats = extract_temperature_stats(
        {
            "stats_data": {
                "Temperature_stats": {
                    "Minimum": 30.1,
                    "Maximum": 42.2,
                    "Mean": 36.3,
                    "Standard_deviation": 2.4,
                }
            }
        }
    )

    assert stats.minimum_c == 30.1
    assert stats.maximum_c == 42.2
    assert stats.mean_c == 36.3
    assert stats.standard_deviation_c == 2.4


def test_extract_temperature_stats_matches_live_lowercase_schema() -> None:
    stats = extract_temperature_stats(
        {
            "stats_data": {
                "temperature_stats": {
                    "minimum": 39.76,
                    "maximum": 39.76,
                    "mean": 39.76,
                    "standard_deviation": 0.0,
                }
            }
        }
    )

    assert stats.minimum_c == 39.76
    assert stats.maximum_c == 39.76
    assert stats.mean_c == 39.76
    assert stats.standard_deviation_c == 0.0
