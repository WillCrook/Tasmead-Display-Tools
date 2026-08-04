from __future__ import annotations

import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
from typing import Sequence
import warnings

from .kml_export import (
    ATR_MAGENTA_TRACK_STYLE,
    KmlCoordinate,
    KmlDocument,
    KmlLineString,
    KmlPlacemark,
    export_kml,
)
from .kml_file_handling import KmlTrack, parse_kml_track


MAX_OUTPUT_COMPONENT_LENGTH = 96
MAX_OUTPUT_STEM_LENGTH = 200
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranspositionJob:
    """One input and its planned output within a transposition batch."""

    input_path: Path
    output_path: Path
    aircraft_name: str
    aircraft_slug: str
    target_airfield_slug: str


@dataclass(frozen=True, slots=True)
class TranspositionPlan:
    """An ordered, immutable set of transposition jobs."""

    output_directory: Path
    jobs: tuple[TranspositionJob, ...]


@dataclass(frozen=True, slots=True)
class TranspositionOutput:
    """A successfully written output, including any runtime collision rename."""

    input_path: Path
    output_path: Path


class TranspositionErrorCode(str, Enum):
    """Stable categories for expected per-file transposition failures."""

    INPUT_KML = "input_kml"
    TRANSFORMATION = "transformation"
    OUTPUT_COLLISION = "output_collision"
    FILESYSTEM_WRITE = "filesystem_write"


@dataclass(frozen=True, slots=True)
class TranspositionError:
    """A safe failure description for one planned input."""

    code: TranspositionErrorCode
    message: str
    input_path: Path
    intended_output_path: Path
    exception_type: str | None = None


class TranspositionFileStatus(str, Enum):
    """Execution status for one planned input."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TranspositionFileOutcome:
    """The result of attempting exactly one transposition job."""

    input_path: Path
    planned_output_path: Path
    final_output_path: Path | None
    status: TranspositionFileStatus
    error: TranspositionError | None = None

    @property
    def output_path(self) -> Path:
        """Return the written output path; valid only for a successful outcome."""
        if self.final_output_path is None:
            raise ValueError("Failed transposition outcomes have no output path.")
        return self.final_output_path


@dataclass(frozen=True, slots=True)
class TranspositionBatchResult:
    """Ordered outcomes for every job in a completed transposition batch."""

    outcomes: tuple[TranspositionFileOutcome, ...]

    @property
    def successful(self) -> tuple[TranspositionFileOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes
            if outcome.status is TranspositionFileStatus.SUCCEEDED
        )

    @property
    def failed_outcomes(self) -> tuple[TranspositionFileOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes
            if outcome.status is TranspositionFileStatus.FAILED
        )

    @property
    def total_count(self) -> int:
        return len(self.outcomes)

    @property
    def success_count(self) -> int:
        return len(self.successful)

    @property
    def failure_count(self) -> int:
        return len(self.failed_outcomes)

    @property
    def succeeded(self) -> bool:
        return self.total_count > 0 and self.failure_count == 0

    @property
    def partially_succeeded(self) -> bool:
        return self.success_count > 0 and self.failure_count > 0

    @property
    def failed(self) -> bool:
        return self.total_count > 0 and self.success_count == 0


def _slug_component(value: str, fallback: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:MAX_OUTPUT_COMPONENT_LENGTH].rstrip("-") or fallback


def _output_stem(
    aircraft_slug: str,
    target_airfield_slug: str,
    sequence: int,
) -> str:
    suffix = "" if sequence == 1 else f"-{sequence}"
    separator = "-at-"
    max_aircraft_length = (
        MAX_OUTPUT_STEM_LENGTH
        - len(separator)
        - len(target_airfield_slug)
        - len(suffix)
    )
    aircraft = aircraft_slug[:max_aircraft_length].rstrip("-") or "aircraft"
    return f"{aircraft}{separator}{target_airfield_slug}{suffix}"


def _next_available_output_path(
    output_directory: Path,
    aircraft_slug: str,
    target_airfield_slug: str,
    occupied_names: set[str],
) -> Path:
    sequence = 1
    while True:
        filename = f"{_output_stem(aircraft_slug, target_airfield_slug, sequence)}.kml"
        if filename.casefold() not in occupied_names:
            return output_directory / filename
        sequence += 1


def create_transposition_plan(
    input_files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...],
    output_directory: str | os.PathLike[str],
    target_airfield: str,
) -> TranspositionPlan:
    """Plan one collision-free output per KML input without writing files."""
    if not input_files:
        raise ValueError("At least one input KML file is required.")

    output_dir = Path(output_directory)
    if not output_dir.is_dir():
        raise ValueError(f'Output directory does not exist: "{output_dir}".')

    occupied_names = {entry.name.casefold() for entry in output_dir.iterdir()}
    target_slug = _slug_component(target_airfield, "airfield")
    jobs: list[TranspositionJob] = []

    for input_file in input_files:
        input_path = Path(input_file)
        if input_path.suffix.lower() != ".kml":
            raise ValueError(f'{input_path.name}: expected a KML file.')

        aircraft_name = input_path.stem
        aircraft_slug = _slug_component(aircraft_name, "aircraft")
        output_path = _next_available_output_path(
            output_dir,
            aircraft_slug,
            target_slug,
            occupied_names,
        )
        occupied_names.add(output_path.name.casefold())
        jobs.append(
            TranspositionJob(
                input_path=input_path,
                output_path=output_path,
                aircraft_name=aircraft_name,
                aircraft_slug=aircraft_slug,
                target_airfield_slug=target_slug,
            )
        )

    return TranspositionPlan(output_directory=output_dir, jobs=tuple(jobs))


# def fetch_single_elevation(coordinate):
#     """
#     Fetch the ground elevation for a single (lat, lon) coordinate.
#     """
#     base_url = "https://api.open-elevation.com/api/v1/lookup"
#     response = requests.get(base_url, params={"locations": f"{coordinate[0]},{coordinate[1]}"})
#     if response.status_code == 200:
#         data = response.json()
#         if data.get('results'):
#             return data['results'][0]['elevation']
#         else:
#             raise Exception("No results in API response.")
#     else:
#         raise Exception(f"API Error: {response.status_code}, {response.text}")


def rotate_route(waypoints, target_lat, target_lon, target_heading):
    """
    Rotate waypoints to align the route with the specified runway heading, accounting for dynamic take-off points.
    """
    if len(waypoints) < 2:
        raise ValueError("At least two waypoints are needed to calculate the initial heading.")

    # Extract the start and second waypoints
    start_lat, start_lon, _ = waypoints[0]
    next_lat, next_lon, _ = waypoints[1]

    # Calculate the scaling factor for longitude based on the starting latitude
    lat_rad = math.radians(start_lat)
    source_lon_scale = math.cos(lat_rad)

    # Calculate the scaling factor for the target latitude
    target_lat_rad = math.radians(target_lat)
    target_lon_scale = math.cos(target_lat_rad)

    # Calculate the current heading (bearing) between the first two waypoints
    delta_lat = next_lat - start_lat
    delta_lon = (next_lon - start_lon) * source_lon_scale
    initial_heading = math.degrees(math.atan2(delta_lon, delta_lat)) % 360

    # Calculate the rotation angle required to align with the target runway heading
    rotation_angle = math.radians(target_heading - initial_heading)

    # Translate all waypoints so the first one matches the target location
    translated_waypoints = [
        (lat - start_lat, lon - start_lon, alt)
        for lat, lon, alt in waypoints
    ]

    # Apply rotation around the origin (which corresponds to the start point)
    rotated_waypoints = []
    for rel_lat, rel_lon, alt in translated_waypoints:
        # Scale longitude for rotation using SOURCE scale (converting to "meters")
        scaled_rel_lon = rel_lon * source_lon_scale

        # Apply rotation around the translated origin
        rotated_lat = (rel_lat * math.cos(rotation_angle) -
                       scaled_rel_lon * math.sin(rotation_angle))
        rotated_scaled_lon = (rel_lat * math.sin(rotation_angle) +
                              scaled_rel_lon * math.cos(rotation_angle))

        # Unscale longitude using TARGET scale and add target location
        final_lat = rotated_lat + target_lat
        final_lon = (rotated_scaled_lon / target_lon_scale) + target_lon

        rotated_waypoints.append((final_lat, final_lon, alt))

    return rotated_waypoints


def write_kml(file_path, coordinates, name_of_aircraft):
    """Create a collision-safe, Google Earth-ready transposed KML output."""
    document = KmlDocument(
        name=f"{name_of_aircraft} Adjusted Coordinates",
        styles=(ATR_MAGENTA_TRACK_STYLE,),
        placemarks=(
            KmlPlacemark(
                name="Path",
                style_url="#magentaTrackLine",
                geometry=KmlLineString(
                    coordinates=tuple(
                        KmlCoordinate(longitude=lon, latitude=lat, altitude_m=alt)
                        for lat, lon, alt in coordinates
                    ),
                    altitude_mode="relativeToGround",
                    extrude_to_ground=True,
                    tessellate=False,
                ),
            ),
        ),
    )
    export_kml(file_path, document, overwrite=False)


def read_config(config_file):
    with open(config_file, "r", encoding="utf-8") as file:
        config = {}
        for line in file:
            line = line.strip()
            if " = " in line:
                key, value = line.split(" = ")
                value = value.strip().replace("\\", "")
                try:
                    config[key] = float(value)
                except ValueError:
                    LOGGER.warning("Invalid value for %s: %s", key, value)
                    continue
        return config


def _waypoints_for_transposition(
    track: KmlTrack,
    ground_reference_elevation: float,
) -> list[tuple[float, float, float]]:
    """Convert the shared KML model to the existing rotation tuple contract."""
    return [
        (
            point.latitude,
            point.longitude,
            point.altitude_m
            if point.altitude_m is not None
            else ground_reference_elevation,
        )
        for point in track.points
    ]


def _runtime_collision_path(
    plan: TranspositionPlan,
    current_index: int,
    successful: list[TranspositionOutput],
) -> Path:
    current_job = plan.jobs[current_index]
    occupied_names = {
        entry.name.casefold() for entry in plan.output_directory.iterdir()
    }
    occupied_names.update(
        job.output_path.name.casefold()
        for job in plan.jobs[current_index + 1:]
    )
    occupied_names.update(
        output.output_path.name.casefold() for output in successful
    )
    return _next_available_output_path(
        plan.output_directory,
        current_job.aircraft_slug,
        current_job.target_airfield_slug,
        occupied_names,
    )


def _failure_outcome(
    job: TranspositionJob,
    output_path: Path,
    code: TranspositionErrorCode,
    error: Exception,
) -> TranspositionFileOutcome:
    if isinstance(error, (ValueError, OSError)):
        message = str(error).strip() or "The file could not be transposed."
    else:
        message = "An unexpected error occurred while processing this file."
    LOGGER.warning(
        "Transposition failed for %s (%s): %s",
        job.input_path,
        code.value,
        message,
        exc_info=(type(error), error, error.__traceback__),
    )
    return TranspositionFileOutcome(
        input_path=job.input_path,
        planned_output_path=job.output_path,
        final_output_path=None,
        status=TranspositionFileStatus.FAILED,
        error=TranspositionError(
            code=code,
            message=message,
            input_path=job.input_path,
            intended_output_path=output_path,
            exception_type=error.__class__.__name__,
        ),
    )


def _run_transposition_plan(
    plan: TranspositionPlan,
    target_lat: float,
    target_lon: float,
    target_heading: float,
    ground_reference_elevation: float,
) -> TranspositionBatchResult:
    outcomes: list[TranspositionFileOutcome] = []
    successful_outputs: list[TranspositionOutput] = []

    for index, job in enumerate(plan.jobs):
        output_path = job.output_path
        LOGGER.info("Transposing %s", job.input_path)
        try:
            track = parse_kml_track(job.input_path)
        except Exception as error:
            outcomes.append(
                _failure_outcome(job, output_path, TranspositionErrorCode.INPUT_KML, error)
            )
            continue

        try:
            waypoints = _waypoints_for_transposition(track, ground_reference_elevation)
            rotated_waypoints = rotate_route(
                waypoints, target_lat, target_lon, target_heading
            )
            adjusted_waypoints = [
                (lat, lon, elevation - ground_reference_elevation)
                for lat, lon, elevation in rotated_waypoints
            ]
        except Exception as error:
            outcomes.append(
                _failure_outcome(job, output_path, TranspositionErrorCode.TRANSFORMATION, error)
            )
            continue

        try:
            write_succeeded = False
            while True:
                try:
                    write_kml(output_path, adjusted_waypoints, job.aircraft_name)
                    write_succeeded = True
                    break
                except FileExistsError:
                    try:
                        output_path = _runtime_collision_path(
                            plan, index, successful_outputs
                        )
                    except Exception as error:
                        outcomes.append(
                            _failure_outcome(
                                job,
                                output_path,
                                TranspositionErrorCode.OUTPUT_COLLISION,
                                error,
                            )
                        )
                        break
        except Exception as error:
            outcomes.append(
                _failure_outcome(
                    job, output_path, TranspositionErrorCode.FILESYSTEM_WRITE, error
                )
            )
            continue

        if not write_succeeded:
            continue
        successful_outputs.append(
            TranspositionOutput(input_path=job.input_path, output_path=output_path)
        )
        outcomes.append(
            TranspositionFileOutcome(
                input_path=job.input_path,
                planned_output_path=job.output_path,
                final_output_path=output_path,
                status=TranspositionFileStatus.SUCCEEDED,
            )
        )
        LOGGER.info("Transposition saved to %s", output_path)

    return TranspositionBatchResult(outcomes=tuple(outcomes))


def _validate_transposition_plan(plan: TranspositionPlan) -> None:
    if not plan.jobs:
        raise ValueError("A transposition plan must contain at least one job.")
    if not plan.output_directory.is_dir():
        raise ValueError(
            f'Output directory does not exist: "{plan.output_directory}".'
        )
    if any(job.output_path.parent != plan.output_directory for job in plan.jobs):
        raise ValueError("Every planned output must be inside the output directory.")


def _legacy_plan(
    input_files: Sequence[str | os.PathLike[str]], output_file: str | os.PathLike[str]
) -> TranspositionPlan:
    if len(input_files) != 1:
        raise ValueError(
            "The deprecated output_file API accepts exactly one input KML file; "
            "use create_transposition_plan() for batch transposition."
        )
    input_path = Path(input_files[0])
    output_path = Path(output_file)
    if input_path.suffix.lower() != ".kml":
        raise ValueError(f"{input_path.name}: expected a KML file.")
    if not output_path.parent.is_dir():
        raise ValueError(f'Output directory does not exist: "{output_path.parent}".')
    return TranspositionPlan(
        output_directory=output_path.parent,
        jobs=(
            TranspositionJob(
                input_path=input_path,
                output_path=output_path,
                aircraft_name=input_path.stem,
                aircraft_slug=_slug_component(input_path.stem, "aircraft"),
                target_airfield_slug="legacy-output",
            ),
        ),
    )


def run_transposition(
    plan: TranspositionPlan | Sequence[str | os.PathLike[str]] | None = None,
    *legacy_args: object,
    target_lat: float | None = None,
    target_lon: float | None = None,
    target_heading: float | None = None,
    ground_reference_elevation: float = 0,
    input_files: Sequence[str | os.PathLike[str]] | None = None,
    output_file: str | os.PathLike[str] | None = None,
) -> TranspositionBatchResult:
    """Run a plan and return an ordered outcome for every planned input.

    The legacy ``input_files``/``output_file`` invocation is supported for one
    input only and is deprecated; new callers must create a plan first.
    """
    if isinstance(plan, TranspositionPlan):
        if input_files is not None or output_file is not None:
            raise TypeError("Do not mix a TranspositionPlan with legacy arguments.")
        positional = list(legacy_args)
        for name in ("target_lat", "target_lon", "target_heading"):
            if locals()[name] is None and positional:
                value = positional.pop(0)
                if name == "target_lat":
                    target_lat = value  # type: ignore[assignment]
                elif name == "target_lon":
                    target_lon = value  # type: ignore[assignment]
                else:
                    target_heading = value  # type: ignore[assignment]
        if positional:
            ground_reference_elevation = positional.pop(0)  # type: ignore[assignment]
        if positional:
            raise TypeError("Too many positional arguments for transposition.")
        active_plan = plan
    else:
        if input_files is not None and plan is not None:
            raise TypeError("Provide legacy input files once.")
        legacy_inputs = input_files if input_files is not None else plan
        positional = list(legacy_args)
        if output_file is None and positional:
            output_file = positional.pop(0)  # type: ignore[assignment]
        for name in ("target_lat", "target_lon", "target_heading"):
            if locals()[name] is None and positional:
                value = positional.pop(0)
                if name == "target_lat":
                    target_lat = value  # type: ignore[assignment]
                elif name == "target_lon":
                    target_lon = value  # type: ignore[assignment]
                else:
                    target_heading = value  # type: ignore[assignment]
        if positional:
            ground_reference_elevation = positional.pop(0)  # type: ignore[assignment]
        if positional or legacy_inputs is None or output_file is None:
            raise TypeError("Legacy transposition requires input_files and output_file.")
        warnings.warn(
            "input_files/output_file transposition is deprecated; create a "
            "TranspositionPlan and pass it as plan= instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        active_plan = _legacy_plan(legacy_inputs, output_file)

    if target_lat is None or target_lon is None or target_heading is None:
        raise TypeError("target_lat, target_lon, and target_heading are required.")
    _validate_transposition_plan(active_plan)
    return _run_transposition_plan(
        active_plan,
        float(target_lat),
        float(target_lon),
        float(target_heading),
        float(ground_reference_elevation),
    )


if __name__ == "__main__":
    try:
        if getattr(sys, "frozen", False):
            app_path = Path(sys.executable).parent
        else:
            app_path = Path(__file__).resolve().parent

        input_dir = app_path / "Input_KML_Files"
        output_dir = app_path / "Output_KML_Files"
        output_dir.mkdir(parents=True, exist_ok=True)

        config = read_config(app_path / "config.txt")
        input_files = sorted(
            path for path in input_dir.iterdir() if path.suffix.lower() == ".kml"
        )
        plan = create_transposition_plan(input_files, output_dir, "Farnborough")
        result = run_transposition(
            plan,
            target_lat=config["target_lat"],
            target_lon=config["target_lon"],
            target_heading=config["target_heading"],
        )
        for output in result.successful:
            print(f"Transposed coordinates saved to {output.output_path}")
        if result.failure_count:
            print(f"{result.failure_count} file(s) failed:")
            for outcome in result.failed_outcomes:
                print(f"{outcome.input_path.name}: {outcome.error.message}")
            raise SystemExit(1)
        print("All files processed successfully.")
    except Exception as error:
        print(f"Error: {error}")
        raise SystemExit(1)
