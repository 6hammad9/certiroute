import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from certiroute.collection import RequestTimeBasis
from certiroute.collection.pair_workflow import ForecastPairManifest
from certiroute.fortyguard.schemas import (
    HeatmapRequest,
    PolygonFeature,
    PolygonFeatureCollection,
    PolygonGeometry,
    SingleHourDateTime,
)
from scripts.manage_forecast_pairs import main


def _future_manifest() -> ForecastPairManifest:
    return ForecastPairManifest(
        request_time_basis=RequestTimeBasis(
            assumption="Test fixed-offset assumption; not vendor-confirmed.",
            utc_offset_minutes=0,
        ),
        requests=(
            HeatmapRequest(
                polygon_aoi=PolygonFeatureCollection(
                    features=[
                        PolygonFeature(
                            geometry=PolygonGeometry(
                                coordinates=[
                                    [
                                        (-112.01, 33.44),
                                        (-112.00, 33.44),
                                        (-112.00, 33.45),
                                        (-112.01, 33.44),
                                    ]
                                ]
                            )
                        )
                    ]
                ),
                date_time=SingleHourDateTime(
                    start_date=date(2099, 8, 22),
                    start_time=time(15),
                ),
                granularity=100,
            ),
        ),
    )


def test_status_json_is_network_free_and_machine_readable(tmp_path, capsys) -> None:
    code = main(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--snapshot-root",
            str(tmp_path / "snapshots"),
            "status",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_forecast_vintages"] == 0
    assert payload["vendor_relative_realization_vintages"] == 0
    assert payload["forecast_time_contract_status"] == ("unverified_caller_assumption")
    assert payload["new_api_submissions_enabled"] is False


def test_live_mode_requires_explicit_task_cap(tmp_path) -> None:
    # Realize needs no date-dependent manifest and reaches the live-cap guard.
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--archive-root",
                str(tmp_path / "archive"),
                "--snapshot-root",
                str(tmp_path / "snapshots"),
                "realize",
                "--live",
            ]
        )

    assert error.value.code == 2


def test_manifest_serialization_keeps_explicit_assumption(tmp_path) -> None:
    manifest = _future_manifest()
    path = tmp_path / "requests.json"
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["request_time_basis"]["source"] == "caller_supplied_assumption"
    assert "not vendor-confirmed" in saved["request_time_basis"]["assumption"]


def test_live_forecast_fails_closed_before_api_configuration(tmp_path, capsys) -> None:
    target = (datetime.now(UTC) + timedelta(hours=2)).replace(second=0, microsecond=0)
    template = _future_manifest()
    request = template.requests[0].model_copy(
        update={
            "date_time": SingleHourDateTime(
                start_date=target.date(), start_time=target.time().replace(tzinfo=None)
            )
        }
    )
    manifest = ForecastPairManifest(
        request_time_basis=template.request_time_basis,
        requests=(request,),
    )
    path = tmp_path / "requests.json"
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    code = main(
        [
            "--archive-root",
            str(tmp_path / "archive"),
            "--snapshot-root",
            str(tmp_path / "snapshots"),
            "forecast",
            str(path),
            "--live",
            "--max-new-tasks",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "BLOCKED:" in captured.err
    assert "timezone used for start_date/start_time" in captured.err
